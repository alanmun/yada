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
import sys
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..config import TARGET_SAMPLE_RATE

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


# Windows exposes one physical microphone through several host APIs -- WASAPI, DirectSound,
# MME, WDM-KS -- so PortAudio enumerates the same device two to four times and the dropdown
# reads as every microphone being duplicated. Preference order, best first: WASAPI is the
# modern path, and MME is last because it truncates device names to 31 characters (which is
# why a stored device name can be cut off mid-word).
_HOSTAPI_PREFERENCE = (
    "Windows WASAPI",
    "Windows DirectSound",
    "Windows WDM-KS",
)
# Below everything, including an API we do not recognise: MME is the legacy path and the
# one that truncates names.
_HOSTAPI_LAST = "MME"

# MME's name limit. Used to match a truncated name against its full-length twin.
_MME_NAME_LIMIT = 31


def _hostapi_rank(name: str) -> int:
    if name == _HOSTAPI_LAST:
        return len(_HOSTAPI_PREFERENCE) + 1
    try:
        return _HOSTAPI_PREFERENCE.index(name)
    except ValueError:
        return len(_HOSTAPI_PREFERENCE)


def _device_key(name: str) -> str:
    """Identity of a physical device, tolerant of MME truncating the name.

    The truncation tolerance is Windows-only, and deliberately so. ALSA device names
    commonly share a long prefix and differ at the very end -- "... (hw:1,0)" against
    "... (hw:2,0)" -- so trimming to 31 characters there would merge two real microphones
    into one and hide a device the user needs.
    """
    normalized = " ".join(name.split()).lower()
    return normalized[:_MME_NAME_LIMIT] if sys.platform == "win32" else normalized


def list_input_devices() -> list[DeviceInfo]:
    """Input devices for the settings dropdown.

    Returns an empty list rather than raising when no audio stack is present -- that is a
    normal state in a container or a fresh WSL session, and the UI should say so calmly.
    """
    try:
        import sounddevice as sd
    except (ImportError, OSError):
        return []
    try:
        default_in = sd.default.device[0]
        devices = sd.query_devices()
        hostapis = [str(api.get("name", "")) for api in sd.query_hostapis()]
    except Exception as exc:
        raise AudioDeviceError(f"Could not enumerate audio devices: {exc}") from exc

    found: list[DeviceInfo] = []
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        api_index = int(dev.get("hostapi", -1))
        found.append(
            DeviceInfo(
                index=idx,
                name=str(dev.get("name", f"device {idx}")),
                channels=int(dev["max_input_channels"]),
                default_samplerate=float(dev.get("default_samplerate") or 0.0),
                is_default=(idx == default_in),
                hostapi=hostapis[api_index] if 0 <= api_index < len(hostapis) else "",
            )
        )

    # One entry per physical device: the best host API for the stream, and the fullest name
    # for the label, since the preferred API is not always the one with the longest name.
    best: dict[str, DeviceInfo] = {}
    labels: dict[str, str] = {}
    defaults: set[str] = set()
    for dev in found:
        key = _device_key(dev.name)
        labels[key] = max(labels.get(key, ""), dev.name, key=len)
        # Being the system default belongs to the device, not to the enumeration that
        # happened to carry the flag -- which is usually MME's, the one that loses on rank.
        # Reading it off the winner alone dropped the marker entirely.
        if dev.is_default:
            defaults.add(key)
        incumbent = best.get(key)
        if incumbent is None or _hostapi_rank(dev.hostapi) < _hostapi_rank(incumbent.hostapi):
            best[key] = dev

    # Sorted by index so the order is stable and matches the host's own enumeration,
    # rather than whatever order the grouping happened to produce.
    return sorted(
        (
            dataclasses.replace(dev, name=labels[key], is_default=key in defaults)
            for key, dev in best.items()
        ),
        key=lambda dev: dev.index,
    )


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


def resolve_device(name: str | None) -> int | None:
    """Map a stored device name back to an index.

    Names are stored rather than indices because indices are reassigned when hardware is
    plugged or unplugged -- storing an index means a headset move silently records from the
    wrong microphone. If the name is gone, fall back to the system default rather than
    failing: recording from the default beats not recording.
    """
    if not name:
        return None
    devices = list_input_devices()
    for dev in devices:
        if dev.name == name:
            return dev.index
    # A name stored before deduplication can be MME's 31-character truncation of the same
    # device. Matching on the shared key keeps an existing setting working rather than
    # silently reassigning the user to the system default.
    key = _device_key(name)
    for dev in devices:
        if _device_key(dev.name) == key:
            return dev.index
    return None


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

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        try:
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            raise AudioDeviceError(
                "No audio backend available (PortAudio could not be loaded)."
            ) from exc

        index = resolve_device(self._device_name)
        try:
            info = sd.query_devices(index if index is not None else sd.default.device[0], "input")
        except Exception as exc:
            raise AudioDeviceError(f"Could not open the microphone: {exc}") from exc

        self._in_rate = int(info["default_samplerate"])
        # Mono where possible; downmix in the callback when the device insists on stereo.
        self._channels = (
            1 if int(info["max_input_channels"]) >= 1 else int(info["max_input_channels"])
        )
        self._resampler = self._make_resampler(self._in_rate)

        try:
            self._stream = sd.InputStream(
                device=index,
                channels=self._channels,
                samplerate=self._in_rate,
                blocksize=self._blocksize,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioDeviceError(f"Could not start recording: {exc}") from exc

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # noqa: BLE001 - closing a dead stream is not fatal
                self.last_error = str(exc)
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
