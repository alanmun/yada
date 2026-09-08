"""Keeping the last few recordings, and the tab that plays them back.

Transcription happens after you stop talking, so a network error at that moment used to
discard a recording that was already complete. This is the store that stops that being a
lost dictation.
"""

from __future__ import annotations

import io
import wave

import pytest

from yada.pipeline.recordings import (
    HARD_CAP,
    Recording,
    RecordingStore,
    new_recording_id,
    now_iso,
)


def wav_bytes(seconds: float = 0.5, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    return RecordingStore(directory=tmp_path / "recordings", keep=3)


def _add(store, transcript="", error="", duration=1.0):
    entry = Recording(
        id=new_recording_id(),
        recorded_at=now_iso(),
        duration_seconds=duration,
        transcript=transcript,
        error=error,
    )
    return store.add(wav_bytes(), entry)


def test_the_newest_are_kept_and_the_oldest_go(store):
    for i in range(5):
        _add(store, transcript=f"text {i}")
    kept = [r.transcript for r in store.entries()]
    assert kept == ["text 4", "text 3", "text 2"], "newest first, oldest pruned"


def test_recordings_made_within_one_second_still_order_correctly(store):
    """Second-resolution timestamps compared equal, so prune deleted arbitrary ones.

    Including, sometimes, the newest -- which is the one the retry shortcut reaches for.
    """
    for i in range(5):
        _add(store, transcript=f"text {i}")
    assert store.latest().transcript == "text 4"


def test_pruning_removes_the_audio_too(store, tmp_path):
    for i in range(5):
        _add(store, transcript=f"text {i}")
    remaining = {r.id for r in store.entries()}
    on_disk = {p.stem for p in (tmp_path / "recordings").glob("*.wav")}
    assert on_disk == remaining, "audio for a pruned recording must not linger"


def test_keeping_none_stores_nothing(tmp_path):
    off = RecordingStore(directory=tmp_path / "recordings", keep=0)
    assert _add(off, transcript="nope") is None
    assert off.entries() == []
    assert not list((tmp_path / "recordings").glob("*.wav"))


def test_the_cap_is_hard(tmp_path):
    """A silly number in a hand-edited settings file must not fill the disk with audio."""
    store = RecordingStore(directory=tmp_path / "recordings", keep=10_000)
    assert store.keep == HARD_CAP


def test_a_failed_transcription_is_recorded_as_such(store):
    entry = _add(store, error="connection reset")
    assert entry is not None
    latest = store.latest()
    assert latest.succeeded is False
    assert "connection reset" in latest.summary()
    assert store.read_audio(latest.id), "the audio is the whole point of keeping it"


def test_a_retry_updates_the_outcome(store):
    _add(store, error="timed out")
    store.update(store.latest().id, transcript="what I actually said", error="")
    assert store.latest().succeeded is True
    assert store.latest().summary() == "what I actually said"


def test_losing_the_index_does_not_lose_the_audio(store, tmp_path):
    """The index is a convenience; the wav files are the data."""
    _add(store, transcript="still here")
    (tmp_path / "recordings" / "index.json").unlink()

    reopened = RecordingStore(directory=tmp_path / "recordings", keep=3)
    entries = reopened.entries()
    assert len(entries) == 1
    assert reopened.read_audio(entries[0].id), "audio recoverable without the index"
    assert "audio is here" in entries[0].summary(), "and it says the details were lost"


def test_a_corrupt_index_is_survivable(store, tmp_path):
    _add(store, transcript="fine")
    (tmp_path / "recordings" / "index.json").write_text("{ not json", encoding="utf-8")
    assert len(RecordingStore(directory=tmp_path / "recordings", keep=3).entries()) == 1


def test_an_index_with_a_byte_order_mark_still_loads(store, tmp_path):
    """Same reason settings.json uses utf-8-sig: a Windows editor adds one."""
    _add(store, transcript="fine")
    index = tmp_path / "recordings" / "index.json"
    index.write_bytes(b"\xef\xbb\xbf" + index.read_bytes())
    entries = RecordingStore(directory=tmp_path / "recordings", keep=3).entries()
    assert entries[0].transcript == "fine"


def test_delete_and_clear(store):
    for i in range(3):
        _add(store, transcript=f"text {i}")
    store.delete(store.latest().id)
    assert len(store.entries()) == 2

    store.clear()
    assert store.entries() == []
    assert store.total_bytes() == 0


def test_a_missing_directory_is_not_an_error(tmp_path):
    store = RecordingStore(directory=tmp_path / "never-created", keep=3)
    assert store.entries() == []
    assert store.latest() is None
    assert store.read_audio("nope") is None


def test_the_summary_is_trimmed_for_a_row(store):
    long_text = "word " * 100
    _add(store, transcript=long_text)
    summary = store.latest().summary(width=40)
    assert len(summary) <= 40
    assert summary.endswith("…")
