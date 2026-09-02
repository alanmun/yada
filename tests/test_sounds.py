"""The chime library: built-ins and your own imports in one list.

The behaviours that matter are the ones that survive an update. Built-in sounds live inside
the versioned install directory, which is replaced wholesale on every release, so anything
that stored a path rather than an id would break on the next update -- and imports kept
anywhere near the install tree would be deleted outright.
"""

from __future__ import annotations

import wave

import pytest

from yada.output import sounds
from yada.pipeline.session import Stage


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Point the sound library at a temp config dir."""
    monkeypatch.setattr(sounds, "config_dir", lambda: tmp_path)
    return tmp_path


def _make_wav(path, *, seconds=0.25, rate=48_000, nchannels=1, sampwidth=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


# --------------------------------------------------------------------------------------
# Enumeration
# --------------------------------------------------------------------------------------


def test_builtins_are_always_present(library):
    ids = [s.id for s in sounds.library()]
    assert "builtin:transcription" in ids
    assert "builtin:transformation" in ids
    assert all(s.builtin for s in sounds.builtin_sounds())


def test_builtins_are_not_removable(library):
    assert sounds.remove_sound("builtin:transcription") is False
    assert sounds.resolve("builtin:transcription") is not None


def test_imports_appear_after_the_builtins(library, tmp_path):
    sounds.import_sound(_make_wav(tmp_path / "src" / "ping.wav"))
    ids = [s.id for s in sounds.library()]
    assert ids[:2] == ["builtin:transcription", "builtin:transformation"]
    assert ids[2] == "custom:ping.wav", "imports follow the built-ins in one blended list"


def test_both_stages_can_use_any_sound(library, tmp_path):
    """Built-ins and imports are interchangeable, including across stages."""
    imported = sounds.import_sound(_make_wav(tmp_path / "src" / "mine.wav"))
    for stage in (Stage.TRANSCRIPTION, Stage.TRANSFORMATION):
        assert sounds.resolve_or_default(imported.id, stage).id == imported.id
        assert sounds.resolve_or_default("builtin:transformation", stage).id == (
            "builtin:transformation"
        )


# --------------------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------------------


def test_import_copies_into_the_config_directory(library, tmp_path):
    source = _make_wav(tmp_path / "elsewhere" / "ping.wav")
    imported = sounds.import_sound(source)

    assert imported.path.parent == sounds.sounds_dir()
    assert imported.path.exists()
    # Copied, not referenced: deleting the original must not break the chime.
    source.unlink()
    assert sounds.resolve(imported.id) is not None


def test_import_survives_a_name_collision(library, tmp_path):
    sounds.import_sound(_make_wav(tmp_path / "a" / "ping.wav"))
    second = sounds.import_sound(_make_wav(tmp_path / "b" / "ping.wav"))
    assert second.path.name == "ping (2).wav"
    assert len(sounds.custom_sounds()) == 2


def test_import_sanitises_hostile_filenames(library, tmp_path):
    source = _make_wav(tmp_path / "src" / "weird.wav")
    imported = sounds.import_sound(source, name="../../etc/passwd")
    assert imported.path.parent == sounds.sounds_dir(), "must not escape the sounds directory"
    assert "/" not in imported.path.name and "\\" not in imported.path.name


def test_import_rejects_a_missing_file(library, tmp_path):
    with pytest.raises(sounds.SoundError, match="does not exist"):
        sounds.import_sound(tmp_path / "nope.wav")


def test_import_rejects_an_unsupported_type(library, tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("not audio")
    with pytest.raises(sounds.SoundError, match="Unsupported file type"):
        sounds.import_sound(bogus)


def test_import_rejects_an_oversized_file(library, tmp_path, monkeypatch):
    monkeypatch.setattr(sounds, "MAX_IMPORT_BYTES", 1024)
    big = _make_wav(tmp_path / "src" / "long.wav", seconds=1.0)
    with pytest.raises(sounds.SoundError, match="larger than"):
        sounds.import_sound(big)


def test_import_rejects_a_corrupt_wav_and_leaves_nothing_behind(library, tmp_path):
    fake = tmp_path / "src" / "broken.wav"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"RIFF not really a wav at all")
    with pytest.raises(sounds.SoundError):
        sounds.import_sound(fake)
    assert sounds.custom_sounds() == [], "a failed import must not leave a partial file"


def test_import_rejects_an_empty_wav(library, tmp_path):
    empty = _make_wav(tmp_path / "src" / "silent.wav", seconds=0)
    with pytest.raises(sounds.SoundError, match="no audio"):
        sounds.import_sound(empty)
    assert sounds.custom_sounds() == []


# --------------------------------------------------------------------------------------
# Removal and fallback
# --------------------------------------------------------------------------------------


def test_removing_an_import_deletes_it(library, tmp_path):
    imported = sounds.import_sound(_make_wav(tmp_path / "src" / "ping.wav"))
    assert sounds.remove_sound(imported.id) is True
    assert not imported.path.exists()
    assert sounds.resolve(imported.id) is None


def test_a_deleted_selection_falls_back_to_the_builtin(library, tmp_path):
    """Silence would be a far more confusing failure than the default chime."""
    imported = sounds.import_sound(_make_wav(tmp_path / "src" / "ping.wav"))
    sounds.remove_sound(imported.id)

    assert sounds.resolve(imported.id) is None
    assert sounds.resolve_or_default(imported.id, Stage.TRANSCRIPTION).id == (
        "builtin:transcription"
    )
    assert sounds.resolve_or_default(imported.id, Stage.TRANSFORMATION).id == (
        "builtin:transformation"
    )


def test_removing_something_that_is_gone_is_not_an_error(library):
    assert sounds.remove_sound("custom:never-existed.wav") is False


def test_duration_is_reported_for_the_ui(library, tmp_path):
    imported = sounds.import_sound(_make_wav(tmp_path / "src" / "ping.wav", seconds=0.5))
    duration = imported.duration_seconds()
    assert duration is not None
    assert 0.45 < duration < 0.55
