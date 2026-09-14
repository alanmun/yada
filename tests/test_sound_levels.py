"""A level of its own for every imported sound.

A single master volume cannot make two imports sit at the same loudness: people bring files
mastered anywhere from a whisper to full scale, so turning the master up for the quiet one
turns the loud one into a jump scare. Each import therefore carries a trim relative to the
master, remembered per file.

The first version of this applied the level as a playback volume, and that does not work:
playback volume tops out at full scale, so with the master at 100% a "200%" level was
bit-for-bit identical to 100%, and a quiet import could not be lifted at all. Boost is now
applied to the samples, which is the only place the loudness actually is, and capped at the
headroom the file has left so it cannot clip. The tests that matter most here are the ones
that would let that regress: a boost that depends on the master volume is the bug.

The rest are the ones that would silently lose someone's work: a level that does not survive
the rebuild after an import, a level left behind in settings for a sound that no longer
exists, and a load being mistaken for an edit -- which would both save and replay a sound
nobody touched.
"""

from __future__ import annotations

import math
import wave

import numpy as np
import pytest

from yada.output import sounds
from yada.output.chime import effective_volume

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from yada.config import Settings
from yada.ui.icons import _close_pixmap
from yada.ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Point both the sound library and its render cache at a temp directory."""
    import yada.config as cfg
    import yada.output.sounds as snd

    monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(snd, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg, "cache_dir", lambda: tmp_path / "cache")
    # Peaks are memoised per file version, and tmp_path is reused across a session.
    monkeypatch.setattr(snd, "_peaks", {})
    return tmp_path


@pytest.fixture
def window(qapp, library):
    w = SettingsWindow(Settings())
    w._imports = library / "originals"  # type: ignore[attr-defined]
    return w


def _make_wav(path, *, peak=0.5, seconds=0.25, rate=48_000, width=2):
    """A tone peaking at `peak` of full scale -- a stand-in for how hot a file was mastered."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tone = np.sin(2 * np.pi * 440 * np.arange(int(rate * seconds)) / rate) * peak
    scale = {1: 127, 2: 32767, 4: 2147483647}[width]
    dtype = {1: "<u1", 2: "<i2", 4: "<i4"}[width]
    samples = (tone * scale + (128 if width == 1 else 0)).round().astype(dtype)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return path


def _peak_of(path) -> float:
    with wave.open(str(path)) as wf:
        raw = np.frombuffer(wf.readframes(wf.getnframes()), "<i2").astype(np.float32)
    return float(np.max(np.abs(raw))) / 32768.0


def _played_peak(sound, gain, master) -> float:
    """The peak actually reaching the speakers: the rendered file times the volume set."""
    path, remainder = sounds.playable(sound, gain)
    return _peak_of(path) * effective_volume(master, remainder)


def _import(window, name: str, *, peak=0.5):
    """Bring a sound into the library the way the import button does."""
    imported = sounds.import_sound(_make_wav(window._imports / f"{name}.wav", peak=peak))
    window.sound_library.refresh()
    return imported


def _rows(window):
    return window.sound_library._visible_rows()


def _row_for(window, sound_id: str):
    return next(row for row in _rows(window) if row.sound.id == sound_id)


# --------------------------------------------------------------------------------------
# What a level actually does to the sound
# --------------------------------------------------------------------------------------


def test_a_boost_does_not_depend_on_the_master_volume(library):
    """The bug this whole mechanism was rebuilt for.

    Applied as a playback volume, a boost had to fit under full scale -- so at master 100%
    there was no room for it at all and 200% was identical to 100%. Amplifying the samples
    is what makes the level mean the same thing wherever the master sits.
    """
    quiet = sounds.import_sound(_make_wav(library / "src" / "quiet.wav", peak=0.1))

    for master in (1.0, 0.8, 0.6):
        ratio = _played_peak(quiet, 2.0, master) / _played_peak(quiet, 1.0, master)
        assert 20 * math.log10(ratio) == pytest.approx(6.0, abs=0.2), (
            f"200% must be twice as loud at master {master:.0%}"
        )


def test_a_quiet_import_can_be_lifted_a_long_way(library):
    """A file mastered at -20 dBFS is the case people actually hit."""
    quiet = sounds.import_sound(_make_wav(library / "src" / "quiet.wav", peak=0.1))
    assert sounds.max_clean_gain(quiet) == pytest.approx(sounds.MAX_BOOST)

    ratio = _played_peak(quiet, 4.0, 1.0) / _played_peak(quiet, 1.0, 1.0)
    assert 20 * math.log10(ratio) == pytest.approx(12.0, abs=0.2)


def test_a_boost_never_clips(library):
    """Amplifying past what a file has room for would distort it, which is worse than quiet."""
    for peak in (0.1, 0.5, 0.9, 0.99):
        sound = sounds.import_sound(_make_wav(library / "src" / f"p{peak}.wav", peak=peak))
        played = sounds.playable(sound, sounds.MAX_BOOST)[0]
        assert _peak_of(played) <= 1.0
        assert _peak_of(played) <= sounds.PEAK_CEILING + 0.02


def test_a_file_already_at_full_scale_offers_no_boost(library):
    """Honest rather than useless: the slider stops instead of pretending to do something."""
    loud = sounds.import_sound(_make_wav(library / "src" / "loud.wav", peak=0.99))
    assert sounds.max_clean_gain(loud) == pytest.approx(1.0)
    assert sounds.playable(loud, 3.0)[0] == loud.path, "nothing to render"


def test_turning_a_sound_down_renders_nothing(library):
    """Attenuation always fits on the playback volume, and baking it into 16-bit samples
    would throw bits away for no gain."""
    sound = sounds.import_sound(_make_wav(library / "src" / "ping.wav", peak=0.5))
    path, remainder = sounds.playable(sound, 0.25)

    assert path == sound.path
    assert remainder == pytest.approx(0.25)
    assert not sounds.levels_dir().exists()


def test_rendering_the_same_level_twice_reuses_the_file(library):
    sound = sounds.import_sound(_make_wav(library / "src" / "ping.wav", peak=0.25))
    first = sounds.playable(sound, 2.0)[0]
    stamped = first.stat().st_mtime_ns
    assert sounds.playable(sound, 2.0)[0] == first
    assert first.stat().st_mtime_ns == stamped, "a cache that rewrites is not a cache"


def test_dragging_a_slider_does_not_fill_the_cache(library):
    """One rendered file per sound, not one per stop the slider passed through."""
    sound = sounds.import_sound(_make_wav(library / "src" / "ping.wav", peak=0.2))
    for percent in range(105, 300, 5):
        sounds.playable(sound, percent / 100)
    assert len(list(sounds.levels_dir().glob("*.wav"))) == 1


def test_every_sample_width_survives_a_boost(library):
    """A WAV copied in directly keeps its own width; 8, 16 and 32-bit all reach here."""
    for width in (1, 2, 4):
        source = _make_wav(library / "src" / f"w{width}.wav", peak=0.2, width=width)
        sound = sounds.import_sound(source, name=f"width {width}")
        played = sounds.playable(sound, 2.0)[0]
        assert played != sound.path, f"{width}-byte samples should have been rendered"
        with wave.open(str(played)) as wf:
            assert wf.getsampwidth() == width


def test_an_unreadable_file_falls_back_to_playing_it_untouched(library, monkeypatch):
    """A volume slider must not be able to raise an exception at a chime."""
    sound = sounds.import_sound(_make_wav(library / "src" / "ping.wav", peak=0.2))
    monkeypatch.setattr(sounds, "_decode", lambda _path: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(sounds, "_peaks", {})

    path, remainder = sounds.playable(sound, 2.0)
    assert path == sound.path
    assert remainder == pytest.approx(2.0)


def test_a_nonsense_level_reads_as_untouched_rather_than_silent(library):
    """settings.json is documented as hand-editable; a missing chime is the harder failure."""
    sound = sounds.import_sound(_make_wav(library / "src" / "ping.wav", peak=0.5))
    assert sounds.playable(sound, "loud")[1] == pytest.approx(1.0)
    assert sounds.playable(sound, None)[1] == pytest.approx(1.0)
    assert effective_volume(0.6, 0.5) == pytest.approx(0.3)
    assert effective_volume(0.6, "loud") == pytest.approx(0.6)


# --------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------


def test_a_level_is_saved_against_its_own_sound(window):
    first = _import(window, "ping")
    second = _import(window, "thud")
    _row_for(window, first.id).level.setValue(40)

    gains = window.collect().output.sound_gains
    assert gains == {first.id: pytest.approx(0.4)}, "only the sound that was adjusted"
    assert second.id not in gains


def test_an_untouched_sound_stores_nothing(window):
    imported = _import(window, "ping")
    assert window.collect().output.sound_gains == {}

    # Moved and moved back is still untouched: the directory is the source of truth for
    # what exists, so settings should not accumulate entries that say nothing.
    row = _row_for(window, imported.id)
    row.level.setValue(30)
    row.level.setValue(100)
    assert window.collect().output.sound_gains == {}


def test_a_level_survives_importing_another_sound(window):
    """Every import rebuilds the rows. A level that did not survive that would look random."""
    first = _import(window, "ping")
    _row_for(window, first.id).level.setValue(25)

    _import(window, "thud")
    assert _row_for(window, first.id).level.value() == 25
    assert window.collect().output.sound_gains[first.id] == pytest.approx(0.25)


def test_removing_a_sound_takes_its_level_with_it(window, monkeypatch):
    from yada.output import sounds

    imported = _import(window, "ping")
    _row_for(window, imported.id).level.setValue(20)
    assert imported.id in window.collect().output.sound_gains

    sounds.remove_sound(imported.id)
    window.sound_library.refresh()
    assert imported.id not in window.collect().output.sound_gains


def test_stored_levels_are_shown_on_load(window):
    imported = _import(window, "ping")
    settings = Settings()
    settings.output.sound_gains = {imported.id: 0.35}

    window.load(settings)
    assert _row_for(window, imported.id).level.value() == 35


def test_a_level_outside_the_slider_is_clamped_rather_than_dropped(window):
    """A hand-edited or future-version config should land somewhere sane, not at zero."""
    imported = _import(window, "ping", peak=0.1)
    settings = Settings()
    settings.output.sound_gains = {imported.id: 12.0}

    window.load(settings)
    row = _row_for(window, imported.id)
    assert row.level.value() == row.ceiling == 400


def test_a_slider_stops_where_its_own_file_stops(window):
    """Every file gets the range it can actually deliver. A uniform range whose top third
    silently did nothing is what made the first version of this look broken."""
    quiet = _import(window, "quiet", peak=0.1)
    middling = _import(window, "middling", peak=0.5)
    loud = _import(window, "loud", peak=0.99)

    assert _row_for(window, quiet.id).ceiling == 400
    assert _row_for(window, middling.id).ceiling == pytest.approx(196, abs=2)
    assert _row_for(window, loud.id).ceiling == 100, "already full scale: down only"


def test_a_slider_that_cannot_boost_says_so(window):
    loud = _row_for(window, _import(window, "loud", peak=0.99).id)
    quiet = _row_for(window, _import(window, "quiet", peak=0.1).id)

    assert "no headroom left" in loud.level.toolTip()
    assert "400%" in quiet.level.toolTip()


# --------------------------------------------------------------------------------------
# Autosave and playback
# --------------------------------------------------------------------------------------


def test_moving_a_level_queues_a_save(window):
    imported = _import(window, "ping")
    window._save_timer.stop()
    _row_for(window, imported.id).level.setValue(55)
    assert window._save_timer.isActive(), "a level is a setting like any other"


def test_moving_a_level_replays_that_sound(window):
    imported = _import(window, "ping")
    heard: list[str] = []
    window.preview_sound_requested.connect(heard.append)

    row = _row_for(window, imported.id)
    row.level.setValue(70)
    assert heard == [], "a drag must not retrigger the sample on every pixel of travel"

    row._replay.timeout.emit()  # the debounce, fired as the timer would
    assert heard == [imported.id], "and then you hear exactly what you just set"


def test_loading_settings_neither_saves_nor_plays(window):
    """Populating widgets programmatically is not an edit; it is the one case autosave
    must not treat as one."""
    imported = _import(window, "ping")
    heard: list[str] = []
    window.preview_sound_requested.connect(heard.append)

    settings = Settings()
    settings.output.sound_gains = {imported.id: 0.45}
    window.load(settings)
    window._save_timer.stop()

    row = _row_for(window, imported.id)
    assert row.level.value() == 45
    assert not row._replay.isActive() and heard == []
    assert not window._save_timer.isActive()


def test_the_master_volume_replay_is_debounced_too(window):
    heard: list[str] = []
    window.preview_sound_requested.connect(heard.append)

    window.chime_volume.slider.setValue(80)
    assert heard == []
    window.chime_volume._replay.timeout.emit()
    assert heard == [window.chime_listening.current_sound()]


# --------------------------------------------------------------------------------------
# The delete button
# --------------------------------------------------------------------------------------


def test_the_remove_glyph_is_drawn_in_the_colour_it_is_given(qapp):
    """Qt's own SP_DialogCloseButton is a near-black X, which on the blue palette is a dark
    glyph on a dark button -- present, but only just."""
    text = QColor("#e8f0fb")
    image = _close_pixmap(text, 24).toImage()

    lit = [
        QColor(image.pixelColor(x, y))
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 200
    ]
    assert lit, "the X must actually be drawn"
    assert all(c.lightness() > 200 for c in lit), "and drawn in the colour asked for"


def test_every_imported_sound_offers_a_remove_button(window):
    imported = _import(window, "ping")
    row = _row_for(window, imported.id)
    assert not row.remove.icon().isNull()
    assert row.remove.accessibleName() == f"Remove {imported.name}"
