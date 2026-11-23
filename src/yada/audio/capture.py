"""Microphone capture, resampled to the app's canonical 24 kHz mono PCM16.

One audio tap, fanned out by the tee. Everything here runs on PortAudio's callback thread,
which has a hard real-time contract: no allocation storms, no network, no locks held across
work. Violating that produces audible dropouts, so the callback does the minimum -- downmix,
resample, convert -- and hands bytes onward.

24 kHz because that is what OpenAI's realtime transcription expects. Batch providers are
handed the same buffer and none of them care.
"""

from __future__ import annotations

import dataclasses
import re
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

import numpy as np

from ..config import TARGET_SAMPLE_RATE
from .hostaudio import prefer_host_alsa

FramesCallback = Callable[[bytes], None]

# ~20 ms at 48 kHz. Small enough that streaming latency is dominated by the network rather
# than by buffering, large enough that the callback overhead stays negligible.
DEFAULT_BLOCKSIZE = 1024


class AudioDeviceError(RuntimeError):
    """The microphone could not be opened. Message is safe to show the user."""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    index: int
    name: str
    channels: int
    default_samplerate: float
    is_default: bool = False
    hostapi: str = ""


_WASAPI = "Windows WASAPI"

# MME's name limit. Used to match a truncated name against its full-length twin.
_MME_NAME_LIMIT = 31


def _device_key(name: str) -> str:
    return " ".join(name.split()).lower()


def list_input_devices() -> list[DeviceInfo]:
    """Input devices for the settings dropdown.

    Returns an empty list rather than raising when no audio stack is present -- that is a
    normal state in a container or a fresh WSL session, and the UI should say so calmly.
    """
    try:
        prefer_host_alsa()
        import sounddevice as sd
    except Exception:  # noqa: BLE001 - import also initializes an unavailable audio stack
        return []
    try:
        default_in = sd.default.device[0]
        devices = sd.query_devices()
        apis = sd.query_hostapis()
        hostapis = [str(api.get("name", "")) for api in apis]
    except Exception as exc:
        raise AudioDeviceError(f"Could not enumerate audio devices: {exc}") from exc

    windows = sys.platform == "win32"
    is_loopback = None
    if windows:
        if _WASAPI not in hostapis:
            return []
        default_in = apis[hostapis.index(_WASAPI)]["default_input_device"]
        # PortAudio builds with loopback support expose playback as inputs. Older DLLs
        # can lack the symbol even when sounddevice's CFFI binding declares it.
        if portaudio := getattr(sd, "_lib", None):
            with suppress(sd._ffi.error):
                is_loopback = getattr(portaudio, "PaWasapi_IsLoopback", None)

    found: list[DeviceInfo] = []
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        api_index = int(dev.get("hostapi", -1))
        hostapi = hostapis[api_index] if 0 <= api_index < len(hostapis) else ""
        # The other Windows APIs expose duplicate aliases and raw driver pins.
        if windows and hostapi != _WASAPI:
            continue
        if is_loopback is not None and is_loopback(idx) == 1:
            continue
        found.append(
            DeviceInfo(
                index=idx,
                name=str(dev.get("name", f"device {idx}")),
                channels=int(dev["max_input_channels"]),
                default_samplerate=float(dev.get("default_samplerate") or 0.0),
                is_default=(idx == default_in),
                hostapi=hostapi,
            )
        )

    # Keep one entry for each distinct saved name, with its longest spelling and default flag.
    by_name: dict[str, DeviceInfo] = {}
    labels: dict[str, str] = {}
    defaults: set[str] = set()
    for dev in found:
        key = _device_key(dev.name)
        labels[key] = max(labels.get(key, ""), dev.name, key=len)
        if dev.is_default:
            defaults.add(key)
            if windows:
                by_name[key] = dev
        by_name.setdefault(key, dev)

    return [
        dataclasses.replace(dev, name=labels[key], is_default=key in defaults)
        for key, dev in by_name.items()
    ]


def peak_level(pcm16: bytes) -> float:
    """Loudest sample in a block, as 0.0-1.0.

    Peak rather than RMS on purpose: the question the meter answers is "am I clipping",
    and RMS hides exactly the short transients that ruin a transcript. Gain has already
    been applied by the time frames reach a sink, so this shows what the model will hear
    rather than what the microphone produced.
    """
    if not pcm16:
        return 0.0
    import numpy as np

    samples = np.frombuffer(pcm16, dtype="<i2")
    if samples.size == 0:
        return 0.0
    # abs(-32768) overflows int16, so widen before taking the magnitude.
    loudest = int(np.abs(samples.astype(np.int32)).max())
    return min(1.0, loudest / 32767.0)


def warm_up() -> None:
    """Initialise PortAudio before the first recording, off the start path.

    `import sounddevice` initialises PortAudio, and enumerating devices walks every endpoint
    the host offers -- neither is free on Windows. Paid on the first keypress it lands
    squarely between the shortcut and the listening chime, which is the one delay the user
    cannot be asked to tolerate: the chime is the only confirmation the shortcut fired.

    Failures are ignored on purpose. This is a cache warm; anything genuinely wrong with the
    audio stack is reported properly when a recording is actually attempted.
    """
    import contextlib

    with contextlib.suppress(Exception):
        list_input_devices()


# ALSA names a card by the slot the kernel gave it, "(hw:5,0)", and slots are handed out
# in the order devices appear. A USB reset, a replug or an audio-service restart can
# renumber them, so a stored "Webcam (hw:5,0)" may now be "Webcam (hw:3,0)" while hw:5,0
# belongs to a different microphone. The slot is never part of a device's identity.
_ALSA_SLOT = re.compile(r"\s*\(hw:\d+,\d+\)\s*$")


def without_slot(name: str) -> str:
    return _ALSA_SLOT.sub("", " ".join(name.split())).lower()


def find_input_device_name(name: str, available: list[str]) -> str | None:
    """Find the current name for a saved microphone."""
    if name in available:
        return name
    key = _device_key(name)
    for candidate in available:
        if _device_key(candidate) == key:
            return candidate
    # Older Windows settings may contain MME's truncated name. Require a unique match.
    if sys.platform == "win32" and len(name) == _MME_NAME_LIMIT and key:
        matches = [candidate for candidate in available if _device_key(candidate).startswith(key)]
        if len(matches) == 1:
            return matches[0]
    # The same microphone under a new ALSA slot number.
    bare = without_slot(name)
    return next((candidate for candidate in available if without_slot(candidate) == bare), None)


def resolve_device(name: str | None, *, devices: list[DeviceInfo] | None = None) -> int | None:
    """Map a stored device name back to an index.

    Names are stored rather than indices because indices are reassigned when hardware is
    plugged or unplugged -- storing an index means a headset move silently records from the
    wrong microphone. If the name is gone, fall back to the system default rather than
    failing: recording from the default beats not recording.
    """
    if not name:
        return None
    if devices is None:
        devices = list_input_devices()
    current = find_input_device_name(name, [dev.name for dev in devices])
    return next((dev.index for dev in devices if dev.name == current), None)


# Streams this process has open. PortAudio can only re-scan devices by shutting down and
# starting again, which would kill an open stream, so a re-scan waits until there are none.
_open_streams = 0
_streams_lock = threading.Lock()


def _rescan_devices(sd) -> None:
    """Make PortAudio see the devices as they are now, not as they were at startup.

    PortAudio enumerates once, when it initialises, and on Linux it addresses ALSA devices
    by slot number. After a renumbering (sleep, replug, audio-service restart) its stale
    table opens whatever now sits in the old slot: the wrong microphone, or one that
    rejects the old device's sample rate. Windows does not need this; WASAPI endpoints are
    stable. About 15 ms on Linux. Caller holds _streams_lock.
    """
    if sys.platform == "win32" or _open_streams:
        return
    terminate = getattr(sd, "_terminate", None)
    initialize = getattr(sd, "_initialize", None)
    if terminate is None or initialize is None:
        return
    try:
        terminate()
    finally:
        initialize()


class AudioCapture:
    """Opens the microphone and emits 24 kHz mono PCM16 frames.

    Resampling uses a *stateful* soxr stream rather than one-shot calls: per-block
    resampling without carried state produces a click at every block boundary.
    """

    def __init__(
        self,
        on_frames: FramesCallback,
        *,
        device: str | None = None,
        gain: float = 1.0,
        blocksize: int = DEFAULT_BLOCKSIZE,
    ) -> None:
        self._on_frames = on_frames
        self._device_name = device
        self._gain = gain
        self._blocksize = blocksize
        self._stream = None
        self._resampler = None
        self._in_rate: int | None = None
        self._channels = 1
        # Set from the callback thread, read from the UI thread. A plain attribute is fine:
        # it is only ever a status string and a lost update is harmless.
        self.last_error: str | None = None
        # Set when the chosen microphone was absent and the system default stood in for
        # it. The setting itself is never changed; this is so the substitution is said out
        # loud rather than discovered from a bad transcript.
        self.substitution: str | None = None

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        try:
            prefer_host_alsa()
            import sounddevice as sd
        except Exception as exc:  # PortAudio can fail during import too
            raise AudioDeviceError(
                "No audio backend available (PortAudio could not be loaded)."
            ) from exc

        global _open_streams
        with _streams_lock:
            try:
                _rescan_devices(sd)
            except Exception as exc:
                raise AudioDeviceError(f"Could not reach the audio system: {exc}") from exc
            windows_devices = list_input_devices() if sys.platform == "win32" else None
            index = resolve_device(self._device_name, devices=windows_devices)
            self.substitution = (
                f"Your microphone ({self._device_name}) is not connected, so this used the "
                "system default instead."
                if self._device_name and index is None
                else None
            )
            if windows_devices is not None and index is None:
                index = next((dev.index for dev in windows_devices if dev.is_default), None)
                if index is None:
                    raise AudioDeviceError(
                        "Windows has no default microphone. Choose one in Windows Settings > "
                        "System > Sound > Input and restart yada, or select an input in yada."
                    )
            try:
                info = sd.query_devices(
                    index if index is not None else sd.default.device[0], "input"
                )
            except Exception as exc:
                raise AudioDeviceError(f"Could not open the microphone: {exc}") from exc

            # Mono where possible; downmix in the callback when the device insists on stereo.
            self._channels = (
                1 if int(info["max_input_channels"]) >= 1 else int(info["max_input_channels"])
            )
            # The device's own rate first. A raw ALSA device may not offer what it reports
            # as its default, so try the common rates too; the resampler covers any of them.
            native = int(info["default_samplerate"])
            rates = [native] + [r for r in (48_000, 44_100, 32_000, 16_000) if r != native]
            first_error: Exception | None = None
            for rate in rates:
                try:
                    stream = sd.InputStream(
                        device=index,
                        channels=self._channels,
                        samplerate=rate,
                        blocksize=self._blocksize,
                        dtype="float32",
                        callback=self._callback,
                    )
                except Exception as exc:  # noqa: BLE001 - sounddevice raises PortAudioError or ValueError; try the next rate
                    first_error = first_error or exc
                    continue
                self._in_rate = rate
                self._resampler = self._make_resampler(rate)
                try:
                    stream.start()
                except Exception as exc:
                    with suppress(Exception):
                        stream.close()
                    self._stream = None
                    raise AudioDeviceError(f"Could not start recording: {exc}") from exc
                self._stream = stream
                _open_streams += 1
                return
            self._stream = None
            raise AudioDeviceError(f"Could not start recording: {first_error}") from first_error

    def stop(self) -> None:
        global _open_streams
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # noqa: BLE001 - closing a dead stream is not fatal
                self.last_error = str(exc)
            finally:
                # Only once it is closed, so a re-scan cannot pull PortAudio out from under it.
                with _streams_lock:
                    _open_streams = max(0, _open_streams - 1)
        # Flush the resampler's tail so the final few milliseconds are not dropped.
        if self._resampler is not None:
            tail = self._flush_resampler()
            if tail:
                self._emit(tail)
            self._resampler = None

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    @property
    def input_samplerate(self) -> int | None:
        return self._in_rate

    # -- resampling ---------------------------------------------------------------------

    def _make_resampler(self, in_rate: int):
        if in_rate == TARGET_SAMPLE_RATE:
            return None  # no conversion needed; skip the work entirely
        import soxr

        return soxr.ResampleStream(in_rate, TARGET_SAMPLE_RATE, 1, dtype="float32", quality="VHQ")

    def _flush_resampler(self) -> bytes:
        if self._resampler is None:
            return b""
        try:
            tail = self._resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
        except Exception:  # noqa: BLE001 - a failed flush costs milliseconds, not the take
            return b""
        return _to_pcm16(tail)

    # -- audio thread -------------------------------------------------------------------

    def _callback(self, indata, _frames, _time, status) -> None:
        if status:
            # Overflows mean the callback is not keeping up. Recorded, not raised: dropping
            # a block is better than tearing down the stream mid-sentence.
            self.last_error = str(status)
        mono = (
            indata[:, 0]
            if indata.ndim > 1 and indata.shape[1] == 1
            else (indata.mean(axis=1) if indata.ndim > 1 else indata)
        )
        samples = np.asarray(mono, dtype=np.float32)
        if self._gain != 1.0:
            samples = samples * self._gain
        if self._resampler is not None:
            samples = self._resampler.resample_chunk(samples)
            if samples.size == 0:
                return
        self._emit(_to_pcm16(samples))

    def _emit(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        try:
            self._on_frames(pcm16)
        except Exception as exc:  # noqa: BLE001 - a sink must never kill the audio thread
            self.last_error = f"audio sink failed: {exc}"


def _to_pcm16(samples: np.ndarray) -> bytes:
    """Float32 [-1, 1] to little-endian PCM16, clipped rather than wrapped.

    Clipping matters: without it, a loud syllable wraps to the opposite polarity and
    produces a burst of noise that the transcription model hears as garbage.
    """
    if samples.size == 0:
        return b""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()
