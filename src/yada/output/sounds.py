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

import re
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path

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
    "transcription": "Rising chime (built-in)",
    "transformation": "Falling chime (built-in)",
}

DEFAULT_FOR_STAGE: dict[Stage, str] = {
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
            f"That WAV file could not be read ({exc}). It may be compressed rather than "
            "plain PCM."
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
        raise SoundError(
            "Could not decode that file. Converting it to a WAV first should work."
        )

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
    try:
        sound.path.unlink()
    except OSError:
        return False
    return True
