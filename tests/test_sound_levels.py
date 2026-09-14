"""A level of its own for every imported sound.

A single master volume cannot make two imports sit at the same loudness: people bring files
mastered anywhere from a whisper to full scale, so turning the master up for the quiet one
turns the loud one into a jump scare. Each import therefore carries a trim relative to the
master, remembered per file.

The behaviours worth pinning down are the ones that would silently lose someone's work: a
level that does not survive the rebuild after an import, a level left behind in settings for
a sound that no longer exists, and a load being mistaken for an edit -- which would both
save and replay a sound nobody touched.
"""

from __future__ import annotations

import wave

import pytest

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
def window(qapp, tmp_path, monkeypatch):
    import yada.config as cfg
    import yada.output.sounds as snd

    monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(snd, "config_dir", lambda: tmp_path)
    w = SettingsWindow(Settings())
    w._imports = tmp_path / "originals"  # type: ignore[attr-defined]
    return w


def _make_wav(path, *, seconds=0.25, rate=48_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def _import(window, name: str):
    """Bring a sound into the library the way the import button does."""
    from yada.output import sounds

    imported = sounds.import_sound(_make_wav(window._imports / f"{name}.wav"))
    window.sound_library.refresh()
    return imported


def _rows(window):
    return window.sound_library._visible_rows()


def _row_for(window, sound_id: str):
    return next(row for row in _rows(window) if row.sound.id == sound_id)


# --------------------------------------------------------------------------------------
# The volume arithmetic
# --------------------------------------------------------------------------------------


def test_the_trim_scales_the_master_volume():
    assert effective_volume(0.6, 0.5) == pytest.approx(0.3)
    assert effective_volume(0.6, 1.0) == pytest.approx(0.6)


def test_a_boost_is_honoured_up_to_full_scale():
    """The master defaults below full scale, so there is real headroom to lift a quiet file."""
    assert effective_volume(0.4, 2.0) == pytest.approx(0.8)
    # Past full scale there is nowhere left to go, and clipping the product is the honest
    # answer rather than handing Qt a volume above 1.0.
    assert effective_volume(0.9, 2.0) == pytest.approx(1.0)


def test_a_nonsense_trim_reads_as_untouched_rather_than_silent():
    """settings.json is documented as hand-editable; a missing chime is the harder failure."""
    assert effective_volume(0.6, "loud") == pytest.approx(0.6)
    assert effective_volume(0.6, None) == pytest.approx(0.6)
    assert effective_volume(0.6, -1.0) == pytest.approx(0.0)


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
    imported = _import(window, "ping")
    settings = Settings()
    settings.output.sound_gains = {imported.id: 12.0}

    window.load(settings)
    assert _row_for(window, imported.id).level.value() == 200


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
