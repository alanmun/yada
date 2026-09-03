"""Resetting settings, which is destructive and must not touch credentials.

Nobody asking to tidy up their preferences means "and log me out of my provider": API keys
live in the OS keyring, are laborious to replace, and are not settings.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from yada.config import Settings


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def yada(qapp, tmp_path, monkeypatch):
    import yada.config as cfg
    from yada.app import YadaApp

    monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr("yada.providers.catalog.cache_dir", lambda: tmp_path)
    app = YadaApp(qapp)
    yield app
    app.overlay.dismiss()
    app.overlay.deleteLater()
    app.tray.hide()
    app.async_thread.stop()
    qapp.processEvents()


def _customise(settings: Settings) -> Settings:
    settings.theme = "system"
    settings.text_scale = 2.0
    settings.hotkey.combo = "ctrl+alt+k"
    settings.transcription.model = "gpt-transcribe"
    settings.output.paste_mode = "after_transcription"
    settings.output.show_overlay = False
    settings.vocabulary.terms = ["yada", "PortAudio"]
    settings.audio.input_gain = 2.5
    return settings


def test_reset_returns_every_tab_to_its_defaults(yada, qapp):
    yada.settings = _customise(Settings())
    yada.show_settings()
    qapp.processEvents()

    yada._reset_settings()
    qapp.processEvents()

    fresh = Settings()
    assert yada.settings.theme == fresh.theme
    assert yada.settings.text_scale == fresh.text_scale
    assert yada.settings.hotkey.combo == fresh.hotkey.combo
    assert yada.settings.transcription.model == fresh.transcription.model
    assert yada.settings.output.paste_mode == fresh.output.paste_mode
    assert yada.settings.output.show_overlay == fresh.output.show_overlay
    assert yada.settings.vocabulary.terms == fresh.vocabulary.terms
    assert yada.settings.audio.input_gain == fresh.audio.input_gain


def test_reset_is_written_to_disk(yada, qapp, tmp_path):
    import yada.config as cfg

    yada.settings = _customise(Settings())
    yada._reset_settings()
    qapp.processEvents()

    assert cfg.load(tmp_path / "settings.json").hotkey.combo == Settings().hotkey.combo


def test_reset_does_not_touch_api_keys(yada, qapp, monkeypatch):
    """The one thing a reset must leave alone."""
    from yada import secrets

    removed: list[str] = []
    monkeypatch.setattr(secrets, "delete_key", lambda pid: removed.append(pid))
    monkeypatch.setattr(secrets, "set_key", lambda pid, key: removed.append(pid))

    yada.settings = _customise(Settings())
    yada._reset_settings()
    qapp.processEvents()

    assert removed == [], "a settings reset is not a credential reset"


def test_the_open_window_shows_the_reset_values(yada, qapp):
    """Otherwise the next autosave writes the pre-reset widget state straight back."""
    yada.settings = _customise(Settings())
    yada.show_settings()
    qapp.processEvents()
    window = yada.settings_window
    assert window is not None

    yada._reset_settings()
    qapp.processEvents()

    fresh = Settings()
    assert window.collect().hotkey.combo == fresh.hotkey.combo
    assert window.collect().output.paste_mode == fresh.output.paste_mode
    assert window.collect().text_scale == pytest.approx(fresh.text_scale)


def test_a_queued_save_cannot_undo_a_reset(yada, qapp):
    yada.show_settings()
    qapp.processEvents()
    window = yada.settings_window
    assert window is not None

    window._save_timer.start()
    assert window._save_timer.isActive()
    window.discard_pending_save()
    assert not window._save_timer.isActive(), (
        "a debounced save still holding the old state would write it back over the reset"
    )


def test_the_reset_button_is_the_last_thing_on_the_system_tab(yada, qapp):
    """Destructive, so it should take scrolling to reach."""
    yada.show_settings()
    qapp.processEvents()
    window = yada.settings_window
    assert window is not None
    assert window.reset_button is not None
    assert window.focus_tab("System")
