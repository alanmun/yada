"""The chime library: built-in sounds and your own imports, in one list.

Design constraints worth stating, because they dictate the shape:

* **Selections are stored as ids, never paths.** The built-in sounds ship inside the
  versioned install directory (`versions/0.1.2/_internal/...`), which is replaced wholesale
  on every update. A stored path would break on the next release.
* **Imports are copied into the config directory.** Referencing the file where the user
  found it means the chime dies the moment they tidy their Downloads folder, and it would
  also be lost on update if it happened to sit in the install tree.
* **The directory is the source of truth**, not a list in settings.json. Enumerating files
  cannot drift out of sync with what is actually on disk, so a sound removed by hand simply
  disappears rather than lingering as a broken entry.
* **Everything is converted to WAV on import.** QSoundEffect keeps short WAVs decoded in
  memory and plays them with minimal latency, which matters for a cue whose whole job is to
  fire the instant something finishes. QMediaPlayer would spin up a pipeline per play and
  add an audible, variable delay.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import config_dir
from ..pipeline.session import Stage

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"

BUILTIN_PREFIX = "builtin:"
CUSTOM_PREFIX = "custom:"

# Formats worth attempting. Anything Qt's decoder handles will work; these are the ones
# people actually have lying around.
IMPORTABLE_SUFFIXES = (".wav", ".mp3", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus", ".wma")

# A chime is a cue, not a track. Longer files still play, but the UI says so.
LONG_SOUND_SECONDS = 4.0
# Refuse the absurd rather than let someone wire a podcast to their hotkey.
MAX_IMPORT_BYTES = 25 * 1024 * 1024


class SoundError(Exception):
    """Import failed. The message is written to be shown to the user verbatim."""


@dataclass(frozen=True, slots=True)
class Sound:
    id: str
    name: str
    path: Path
    builtin: bool

    @property
    def removable(self) -> bool:
        return not self.builtin

    def duration_seconds(self) -> float | None:
        try:
            with wave.open(str(self.path)) as wf:
                rate = wf.getframerate()
                return wf.getnframes() / rate if rate else None
        except (OSError, wave.Error):
            return None


BUILTINS: dict[str, str] = {
    # id suffix -> display name
    "listening": "Single tap (built-in)",
    "transcription": "Rising chime (built-in)",
    "transformation": "Falling chime (built-in)",
}

DEFAULT_FOR_STAGE: dict[Stage, str] = {
    Stage.LISTENING: f"{BUILTIN_PREFIX}listening",
    Stage.TRANSCRIPTION: f"{BUILTIN_PREFIX}transcription",
    Stage.TRANSFORMATION: f"{BUILTIN_PREFIX}transformation",
}


def sounds_dir() -> Path:
    """Where imported sounds live. Outside the install tree, so updates never touch them."""
    return config_dir() / "sounds"


# --------------------------------------------------------------------------------------
# Enumeration
# --------------------------------------------------------------------------------------


def builtin_sounds() -> list[Sound]:
    out: list[Sound] = []
    for key, name in BUILTINS.items():
        path = BUILTIN_DIR / f"{key}.wav"
        if path.exists():
            out.append(Sound(id=f"{BUILTIN_PREFIX}{key}", name=name, path=path, builtin=True))
    return out


def custom_sounds() -> list[Sound]:
    directory = sounds_dir()
    if not directory.is_dir():
        return []
    found = [
        Sound(id=f"{CUSTOM_PREFIX}{p.name}", name=p.stem, path=p, builtin=False)
        for p in sorted(directory.iterdir())
        if p.is_file() and p.suffix.lower() == ".wav"
    ]
    return sorted(found, key=lambda s: s.name.lower())


def library() -> list[Sound]:
    """Built-ins first, then imports, as one list -- the two are interchangeable."""
    return [*builtin_sounds(), *custom_sounds()]


def resolve(sound_id: str) -> Sound | None:
    return next((s for s in library() if s.id == sound_id), None)


def resolve_or_default(sound_id: str, stage: Stage) -> Sound | None:
    """Fall back to the stage's built-in when a selection has gone missing.

    Happens when an imported sound is deleted, or a config file is carried to another
    machine. Silence would be a confusing failure, so the default is used instead.
    """
    return resolve(sound_id) or resolve(DEFAULT_FOR_STAGE[stage])


# --------------------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------------------


def _safe_stem(name: str) -> str:
    """A filename that is safe on both platforms and still recognisable."""
    cleaned = re.sub(r"[^\w \-.]", "", name, flags=re.UNICODE).strip(" .-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "sound")[:60]


def _unique_destination(stem: str) -> Path:
    directory = sounds_dir()
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}.wav"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({counter}).wav"
        counter += 1
    return candidate


def _validate_wav(path: Path) -> None:
    """Reject a file QSoundEffect would silently refuse to play."""
    try:
        with wave.open(str(path)) as wf:
            if wf.getnframes() == 0:
                raise SoundError("That file contains no audio.")
            if wf.getsampwidth() not in (1, 2, 3, 4):
                raise SoundError("That WAV file uses an unsupported sample format.")
    except wave.Error as exc:
        raise SoundError(
            f"That WAV file could not be read ({exc}). It may be compressed rather than plain PCM."
        ) from exc
    except OSError as exc:
        raise SoundError(f"Could not read that file ({exc}).") from exc


def import_sound(source: Path, *, name: str | None = None) -> Sound:
    """Bring a sound into the library, converting to WAV if needed.

    Raises SoundError with a message intended for the user.
    """
    source = Path(source)
    if not source.is_file():
        raise SoundError("That file does not exist.")
    if source.stat().st_size > MAX_IMPORT_BYTES:
        raise SoundError(
            f"That file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB. "
            "A chime should be a second or two."
        )
    if source.suffix.lower() not in IMPORTABLE_SUFFIXES:
        supported = ", ".join(s.lstrip(".") for s in IMPORTABLE_SUFFIXES)
        raise SoundError(f"Unsupported file type. Try one of: {supported}.")

    destination = _unique_destination(_safe_stem(name or source.stem))

    if source.suffix.lower() == ".wav":
        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            raise SoundError(f"Could not copy that file ({exc}).") from exc
        try:
            _validate_wav(destination)
        except SoundError:
            destination.unlink(missing_ok=True)
            raise
    else:
        try:
            convert_to_wav(source, destination)
        except SoundError:
            destination.unlink(missing_ok=True)
            raise

    return Sound(
        id=f"{CUSTOM_PREFIX}{destination.name}",
        name=destination.stem,
        path=destination,
        builtin=False,
    )


def convert_to_wav(source: Path, destination: Path) -> None:
    """Decode `source` to a PCM WAV using Qt's audio decoder.

    Done once, at import, so playback stays on the low-latency QSoundEffect path forever
    after. Qt's FFmpeg backend handles the common compressed formats.
    """
    try:
        from PySide6.QtCore import QEventLoop, QTimer, QUrl
        from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat
    except ImportError as exc:  # pragma: no cover - Qt is a hard dependency of the app
        raise SoundError(f"Audio conversion is unavailable ({exc}).") from exc

    fmt = QAudioFormat()
    fmt.setSampleRate(48_000)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

    decoder = QAudioDecoder()
    decoder.setAudioFormat(fmt)
    decoder.setSource(QUrl.fromLocalFile(str(source)))

    chunks: list[bytes] = []
    failure: list[str] = []
    loop = QEventLoop()

    def on_buffer_ready() -> None:
        buffer = decoder.read()
        if buffer.isValid():
            chunks.append(bytes(buffer.constData()))

    def on_finished() -> None:
        loop.quit()

    def on_error(*_args) -> None:
        failure.append(decoder.errorString() or "the decoder reported an error")
        loop.quit()

    decoder.bufferAvailableChanged.connect(lambda ready: ready and on_buffer_ready())
    decoder.finished.connect(on_finished)
    decoder.error.connect(on_error)

    # Never hang a settings dialog on a malformed file.
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(20_000)

    decoder.start()
    loop.exec()
    decoder.stop()

    if failure:
        raise SoundError(f"Could not decode that file: {failure[0]}")
    if not chunks:
        raise SoundError("Could not decode that file. Converting it to a WAV first should work.")

    pcm = b"".join(chunks)
    try:
        with wave.open(str(destination), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48_000)
            wf.writeframes(pcm)
    except (OSError, wave.Error) as exc:
        raise SoundError(f"Could not write the converted sound ({exc}).") from exc


# --------------------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------------------


def remove_sound(sound_id: str) -> bool:
    """Delete an imported sound. Built-ins are not removable; returns False for those."""
    sound = resolve(sound_id)
    if sound is None or sound.builtin:
        return False
    stamp = _stamp(sound.path)
    try:
        sound.path.unlink()
    except OSError:
        return False
    # Its rendered levels are derived from a file that no longer exists.
    _prune_levels(keep=None, stamp=stamp)
    return True


# --------------------------------------------------------------------------------------
# Levels
#
# A per-sound level cannot be a playback volume. QSoundEffect's volume tops out at full
# scale, so with the master anywhere near 100% there is no headroom left to lift a quiet
# import with -- at master 100% a "200%" trim is bit-for-bit identical to 100%. Boost is
# therefore applied to the samples, which is the only place the loudness actually is.
#
# Attenuation stays on the playback volume: it always fits, and baking it into 16-bit
# samples would throw away bits for nothing. So a level below 100% writes no file at all.
#
# How far a file can be lifted is a property of the file: one already peaking at full scale
# has nowhere to go, one peaking at -12 dBFS has four times. That headroom is measured and
# the boost is capped at it, so a level never clips -- it just stops getting louder, and the
# UI stops offering more.
# --------------------------------------------------------------------------------------

# Rendered copies are derived data: deleting them costs one recomputation, so they live in
# the cache directory rather than beside the imports they came from.
LEVELS_DIRNAME = "levels"

# Past this, boosting a chime is not a level any more. Also the point where a file quiet
# enough to need it is usually quiet for a reason -- noise comes up with the signal.
MAX_BOOST = 4.0

# Leaves the peak a hair below full scale. Sample-domain gain is exact, but resampling in
# the audio stack downstream can overshoot a touch, and an inaudible margin is cheaper than
# finding out which backends do.
PEAK_CEILING = 0.98


def levels_dir() -> Path:
    from ..config import cache_dir

    return cache_dir() / LEVELS_DIRNAME


def _stamp(path: Path) -> str:
    """Identifies the exact bytes on disk, so a re-import under the same name re-renders."""
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    digest = hashlib.sha1(
        f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode(), usedforsecurity=False
    )
    return digest.hexdigest()[:12]


def _decode(path: Path) -> tuple[Any, wave._wave_params]:
    """WAV samples as float in -1..1, whatever width they were stored at.

    Imports are converted to 16-bit on the way in, but a WAV copied in directly keeps its
    own width, and the validator accepts 8, 16, 24 and 32-bit. All four appear here.
    """
    import numpy as np

    with wave.open(str(path)) as wf:
        params = wf.getparams()
        raw = wf.readframes(wf.getnframes())

    width = params.sampwidth
    if width == 1:
        # 8-bit WAV is unsigned, centred on 128.
        samples = (np.frombuffer(raw, "<u1").astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        samples = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 3:
        # No 24-bit dtype exists, so the three bytes are reassembled and sign-extended.
        usable = len(raw) - (len(raw) % 3)
        trio = np.frombuffer(raw[:usable], np.uint8).reshape(-1, 3).astype(np.int32)
        packed = trio[:, 0] | (trio[:, 1] << 8) | (trio[:, 2] << 16)
        samples = np.where(packed & 0x800000, packed - 0x1000000, packed).astype(np.float32)
        samples /= 8388608.0
    elif width == 4:
        samples = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise SoundError(f"Unsupported sample width: {width} bytes.")
    return samples, params


def _encode(samples: Any, params: wave._wave_params, destination: Path) -> None:
    import numpy as np

    clipped = np.clip(samples, -1.0, 1.0)
    width = params.sampwidth
    if width == 1:
        raw = (clipped * 127.0 + 128.0).round().clip(0, 255).astype("<u1").tobytes()
    elif width == 2:
        raw = (clipped * 32767.0).round().clip(-32768, 32767).astype("<i2").tobytes()
    elif width == 3:
        as32 = (clipped * 8388607.0).round().clip(-8388608, 8388607).astype("<i4")
        raw = as32.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    else:
        raw = (clipped * 2147483647.0).round().clip(-2147483648, 2147483647).astype("<i4").tobytes()

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a half-written file here is a chime that does not play.
    tmp = destination.with_suffix(".wav.tmp")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(params.nchannels)
        wf.setsampwidth(width)
        wf.setframerate(params.framerate)
        wf.writeframes(raw)
    tmp.replace(destination)


_peaks: dict[tuple[str, str], float] = {}


def peak_level(sound: Sound) -> float:
    """The loudest sample in the file, 0..1. Measured once per version of the file."""
    key = (str(sound.path), _stamp(sound.path))
    if key not in _peaks:
        try:
            import numpy as np

            samples, _ = _decode(sound.path)
            _peaks[key] = float(np.max(np.abs(samples))) if samples.size else 0.0
        except (OSError, wave.Error, SoundError, ValueError):
            # Unreadable here means unplayable later, and the caller's fallback for that is
            # better than an exception from a volume slider. A peak of 1.0 offers no boost,
            # which is the safe answer.
            _peaks[key] = 1.0
    return _peaks[key]


def max_clean_gain(sound: Sound) -> float:
    """How far this file can be lifted before it would clip, as a multiplier from 1.0.

    A silent or unreadable file reports 1.0: there is nothing to make louder.
    """
    peak = peak_level(sound)
    if peak <= 0.0:
        return 1.0
    return max(1.0, min(MAX_BOOST, PEAK_CEILING / peak))


def playable(sound: Sound, gain: float) -> tuple[Path, float]:
    """The file to play for `sound` at `gain`, and the volume multiplier still to apply.

    Boost is baked into a rendered copy, up to what the file has headroom for; whatever is
    left over -- attenuation, or a boost beyond the file's headroom -- comes back as a
    multiplier for the caller to put on the playback volume, where it costs nothing.
    """
    try:
        wanted = float(gain)
    except (TypeError, ValueError):
        wanted = 1.0
    wanted = max(0.0, wanted)
    baked = max(1.0, min(wanted, max_clean_gain(sound)))
    remainder = wanted / baked if baked else wanted

    if baked <= 1.0:
        return sound.path, remainder
    rendered = _render(sound, baked)
    return (rendered, remainder) if rendered else (sound.path, wanted)


def _render(sound: Sound, gain: float) -> Path | None:
    """Write (or reuse) a copy of `sound` amplified by `gain`. None if it could not be."""
    stamp = _stamp(sound.path)
    destination = levels_dir() / f"{stamp}-{round(gain * 100)}.wav"
    if destination.is_file():
        return destination
    try:
        samples, params = _decode(sound.path)
        _encode(samples * gain, params, destination)
    except (OSError, wave.Error, SoundError, ValueError):
        return None
    _prune_levels(keep=destination, stamp=stamp)
    return destination


def _prune_levels(*, keep: Path | None, stamp: str) -> None:
    """Drop this sound's other rendered levels. Dragging a slider makes one per stop."""
    directory = levels_dir()
    if not directory.is_dir():
        return
    for path in directory.glob(f"{stamp}-*.wav"):
        if path != keep:
            path.unlink(missing_ok=True)
