"""The last few recordings, kept so a failed transcription is not a lost dictation.

The problem this solves: transcription happens *after* you stop talking, so a network
error at that moment threw away everything you had just said. The audio existed, was
complete, and was discarded because the request that consumed it failed.

So every recording is written to disk with the outcome of its transcription, and the last
few are retained. A failure becomes something to retry rather than something to repeat.

This is audio of everything dictated, held on disk, which is a real change in what yada
keeps. Hence: a hard cap on how many, a setting to keep none at all, the count stated
plainly in the UI, and a way to delete them all at once.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import cache_dir

INDEX_NAME = "index.json"
SCHEMA_VERSION = 1
# Retained regardless of the setting, so a runaway value cannot fill a disk with audio.
HARD_CAP = 20


@dataclass(slots=True)
class Recording:
    """One recording and what came of transcribing it."""

    id: str
    recorded_at: str
    duration_seconds: float
    transcript: str = ""
    error: str = ""
    model: str = ""
    provider: str = ""
    streamed: bool = False

    @property
    def succeeded(self) -> bool:
        return bool(self.transcript)

    @property
    def when(self) -> str:
        """Local time, for a row in a list."""
        try:
            moment = datetime.fromisoformat(self.recorded_at)
        except ValueError:
            return self.recorded_at
        return moment.astimezone().strftime("%d %b %H:%M:%S")

    def summary(self, width: int = 70) -> str:
        if self.transcript:
            text = " ".join(self.transcript.split())
            return text if len(text) <= width else text[: width - 1] + "…"
        return self.error or "not transcribed"


@dataclass(slots=True)
class RecordingStore:
    """Recordings on disk, newest first, capped.

    The index is a convenience, not the source of truth: it is rebuilt from the `.wav`
    files if it goes missing or unreadable, because losing an index must not lose audio
    the user may be about to retry.
    """

    directory: Path = field(default_factory=lambda: cache_dir() / "recordings")
    keep: int = 5

    def __post_init__(self) -> None:
        self.keep = max(0, min(int(self.keep), HARD_CAP))

    # -- paths --------------------------------------------------------------------------

    def _index_path(self) -> Path:
        return self.directory / INDEX_NAME

    def audio_path(self, recording_id: str) -> Path:
        return self.directory / f"{recording_id}.wav"

    # -- reads --------------------------------------------------------------------------

    def entries(self) -> list[Recording]:
        """Newest first. Only recordings whose audio is actually present."""
        known = {r.id: r for r in self._load_index()}
        found = []
        for wav in self.directory.glob("*.wav"):
            entry = known.get(wav.stem)
            if entry is None:
                # Audio with no index row: describe what can be known rather than hide it.
                entry = Recording(
                    id=wav.stem,
                    recorded_at=_iso_from_mtime(wav),
                    duration_seconds=_duration_of(wav),
                    error="details were lost, but the audio is here",
                )
            found.append(entry)
        # Sorted on (time, id) for a total order. Second resolution was not enough: five
        # recordings inside one second compared equal, so the order was arbitrary and
        # `prune` deleted whichever ones it happened to see last -- including the newest,
        # which is the one the retry shortcut reaches for.
        found.sort(key=lambda r: (r.recorded_at, r.id), reverse=True)
        return found

    def latest(self) -> Recording | None:
        entries = self.entries()
        return entries[0] if entries else None

    def read_audio(self, recording_id: str) -> bytes | None:
        try:
            return self.audio_path(recording_id).read_bytes()
        except OSError:
            return None

    # -- writes -------------------------------------------------------------------------

    def add(self, wav_bytes: bytes, recording: Recording) -> Recording | None:
        """Store audio and its outcome. Returns the stored entry, or None if disabled."""
        if self.keep <= 0 or not wav_bytes:
            return None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.audio_path(recording.id).write_bytes(wav_bytes)
        except OSError:
            return None
        rows = [r for r in self._load_index() if r.id != recording.id]
        rows.append(recording)
        self._save_index(rows)
        self.prune()
        return recording

    def update(self, recording_id: str, **changes) -> None:
        """Record a later attempt against an existing recording."""
        rows = self._load_index()
        for row in rows:
            if row.id == recording_id:
                for key, value in changes.items():
                    setattr(row, key, value)
                break
        else:
            return
        self._save_index(rows)

    def delete(self, recording_id: str) -> None:
        with contextlib.suppress(OSError):
            self.audio_path(recording_id).unlink(missing_ok=True)
        self._save_index([r for r in self._load_index() if r.id != recording_id])

    def clear(self) -> None:
        for wav in list(self.directory.glob("*.wav")):
            with contextlib.suppress(OSError):
                wav.unlink()
        self._save_index([])

    def prune(self) -> None:
        """Drop everything past `keep`, audio included."""
        for stale in self.entries()[self.keep :]:
            self.delete(stale.id)

    def total_bytes(self) -> int:
        total = 0
        for wav in self.directory.glob("*.wav"):
            with contextlib.suppress(OSError):
                total += wav.stat().st_size
        return total

    # -- index --------------------------------------------------------------------------

    def _load_index(self) -> list[Recording]:
        try:
            # utf-8-sig, for the same reason settings.json uses it: a file a user may open
            # in a Windows editor comes back with a byte-order mark.
            raw = json.loads(self._index_path().read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
            return []
        known = {f.name for f in Recording.__dataclass_fields__.values()}
        rows = []
        for row in raw.get("recordings") or []:
            if isinstance(row, dict) and row.get("id"):
                rows.append(Recording(**{k: v for k, v in row.items() if k in known}))
        return rows

    def _save_index(self, rows: list[Recording]) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "recordings": [asdict(r) for r in rows],
        }
        with contextlib.suppress(OSError):
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = self._index_path().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self._index_path())


def new_recording_id() -> str:
    """Sortable and human-readable, so a directory listing is already chronological.

    Microseconds rather than seconds, because two recordings in the same second must not
    compare equal -- see the sort in `entries`.
    """
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _iso_from_mtime(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return now_iso()
    return datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds")


def _duration_of(path: Path) -> float:
    """Length from the WAV header, without decoding the audio."""
    import wave

    try:
        with contextlib.closing(wave.open(str(path), "rb")) as handle:
            rate = handle.getframerate() or 1
            return handle.getnframes() / rate
    except Exception:  # noqa: BLE001 - a malformed header must not hide the recording
        return 0.0
