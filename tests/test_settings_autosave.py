"""Autosave, and the conditional restart action.

There is no Save button: every change is written as it is made. The interesting cases are
the ones where that is *wrong* -- populating widgets programmatically, scrolling, and typing
a half-finished keyboard shortcut.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QScrollBar

from yada.config import Settings
from yada.ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    import yada.config as cfg
    import yada.output.sounds as snd

    monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(snd, "config_dir", lambda: tmp_path)
    w = SettingsWindow(Settings())
    saves: list[Settings] = []
    w.saved.connect(saves.append)
    w._saves = saves  # type: ignore[attr-defined]
    return w


def _commit(window) -> Settings | None:
    """Fire the debounce immediately, as the timer would."""
    if window._save_timer.isActive():
        window._save_timer.stop()
        window._commit()
    return window._saves[-1] if window._saves else None


def _shown(widget) -> bool:
    """The widget's own visibility flag.

    isVisible() is False whenever any ancestor is unshown, which is always the case in a
    headless test, so it says nothing about what the code asked for.
    """
    return not widget.isHidden()


def test_there_is_no_save_button(window):
    assert not hasattr(window, "save_button")


def test_toggling_a_checkbox_saves(window):
    window.always_copy.setChecked(False)
    saved = _commit(window)
    assert saved is not None
    assert saved.output.always_copy_to_clipboard is False


def test_changing_a_combo_saves(window):
    window._select(window.paste_mode, "after_transcription")
    window.paste_mode.setCurrentIndex(window.paste_mode.findData("after_transformation"))
    saved = _commit(window)
    assert saved is not None
    assert saved.output.paste_mode == "after_transformation"


def test_loading_does_not_trigger_a_save(window):
    """load() populates every widget; saving those values back would be pointless churn
    and would overwrite settings with what was just read."""
    window._saves.clear()
    settings = Settings()
    settings.vocabulary.terms = ["Troutwood"]
    window.load(settings)
    assert window._save_timer.isActive() is False
    assert window._saves == []


def test_scrolling_does_not_trigger_a_save(window):
    """A scroll bar is a QAbstractSlider, which the autosave wiring would otherwise catch."""
    window._saves.clear()
    bars = window.findChildren(QScrollBar)
    assert bars, "sanity: the settings page scrolls"
    for bar in bars:
        bar.setValue(bar.maximum())
    assert window._save_timer.isActive() is False
    assert window._saves == []


def test_an_invalid_shortcut_is_reported_and_not_saved(window):
    """Autosave means every keystroke of 'ctrl+shift+;' is seen, including the broken
    intermediate states. Saving those would apply and fail on each one."""
    window.hotkey_field.setText("ctrl+shift+;")
    _commit(window)

    window.hotkey_field.setText("ctrl+shift+")  # mid-typing
    assert _shown(window.hotkey_error)
    saved = _commit(window)
    assert saved is not None
    assert saved.hotkey.combo == "ctrl+shift+;", "must keep the last shortcut that parsed"

    window.hotkey_field.setText("alt+f5")
    assert not _shown(window.hotkey_error)
    assert _commit(window).hotkey.combo == "alt+f5"


def test_closing_flushes_a_pending_save(window):
    window.always_copy.setChecked(False)
    assert window._save_timer.isActive(), "sanity: the save is still debounced"
    window.flush_pending_save()
    assert window._saves, "closing must not discard an in-flight change"


def test_restart_button_only_appears_with_a_staged_update(window):
    assert _shown(window.restart_button) is False
    window.set_update_ready("0.2.0")
    assert _shown(window.restart_button) is True
    assert "0.2.0" in window.restart_button.text()
    window.set_update_ready(None)
    assert _shown(window.restart_button) is False


def test_focus_tab_finds_tabs_by_label(window):
    """By label, not index. The tab order has changed twice already, and a hardcoded
    index silently selects the wrong page the next time it moves."""
    assert window.focus_tab("Updates") is True
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Updates"

    # Labels carry Qt's mnemonic escaping; callers should not have to know that.
    assert window.focus_tab("Audio & output") is True
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Audio && output"

    assert window.focus_tab("Providers") is True
    assert window.focus_tab("No Such Tab") is False


def test_reopening_the_window_does_not_discard_a_pending_edit(window, monkeypatch):
    """show_settings reloads every field from settings, so anything still sitting in the
    autosave debounce has to be written first."""
    window.always_copy.setChecked(False)
    assert window._save_timer.isActive(), "sanity: the change is still debounced"

    window.flush_pending_save()
    assert window._saves, "the pending change must be committed, not dropped"
    assert window._saves[-1].output.always_copy_to_clipboard is False
