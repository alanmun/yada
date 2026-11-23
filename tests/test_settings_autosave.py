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
    settings.vocabulary.terms = ["Kubernetes"]
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
    assert window.focus_tab("System") is True
    assert window.tabs.tabText(window.tabs.currentIndex()) == "System"

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


# --------------------------------------------------------------------------------------
# Autosave must not feed itself
# --------------------------------------------------------------------------------------


def _text_models(*ids):
    from yada.providers.base import Modality, ModelInfo

    return [ModelInfo(id=i, provider="fake", modality=Modality.TEXT) for i in ids]


def test_repopulating_a_model_list_does_not_schedule_a_save(qapp, window, monkeypatch):
    """The flicker bug, stated as the loop it was.

    `findChildren` returns a ModelPicker *and* the QComboBox and QLineEdit inside it, so
    the inner line edit had `textChanged` wired to autosave -- and blocking signals on the
    combo does not block the line edit, they are different objects. Repopulating scheduled
    a save; the app pushes fresh state into the window from its save handler; that
    repopulated the list again. About three times a second, indefinitely, which destroyed
    the popup under the pointer every time.
    """
    scheduled = []
    monkeypatch.setattr(window, "_schedule_save", lambda *_a: scheduled.append(1))

    window.tf_model.set_models(_text_models("a", "b"), current="a")
    window.stt_model.set_models(_text_models("c", "d"), current="")
    qapp.processEvents()

    assert scheduled == [], "syncing the UI from settings is not the user editing it"


def test_pushing_capabilities_does_not_schedule_a_save(qapp, window, monkeypatch):
    """The same loop by the other route: the effort list is rebuilt from the save handler."""
    from yada.providers.base import ReasoningEffort, Support

    scheduled = []
    monkeypatch.setattr(window, "_schedule_save", lambda *_a: scheduled.append(1))

    window.set_transform_capabilities(
        reasoning=Support.SUPPORTED,
        efforts=tuple(ReasoningEffort),
        priority=Support.SUPPORTED,
    )
    qapp.processEvents()
    assert scheduled == []


def test_a_user_choosing_a_model_still_saves(qapp, window, monkeypatch):
    """The fix must not go so far that a real selection stops being recorded."""
    window.tf_model.set_models(_text_models("a", "b"), current="a")
    scheduled = []
    monkeypatch.setattr(window, "_schedule_save", lambda *_a: scheduled.append(1))

    window.tf_model.combo.setCurrentIndex(window.tf_model.combo.findData("b"))
    qapp.processEvents()

    assert scheduled, "picking a different model is an edit and must be saved"
    assert window.tf_model.current_model() == "b"


def test_a_refresh_arriving_mid_selection_waits_for_the_popup_to_close(qapp, window):
    """Rebuilding an open list destroys the item under the pointer."""
    window.tf_model.set_models(_text_models("a", "b"), current="a")
    combo = window.tf_model.combo

    combo.showPopup()
    qapp.processEvents()
    if not combo.view().isVisible():
        pytest.skip("this platform will not show a combo popup offscreen")

    # `current` is in the new list, so the count is exactly the models -- a pinned
    # "not in the discovered list" entry would otherwise make this read as off-by-one.
    window.tf_model.set_models(_text_models("x", "y", "z"), current="x")
    assert combo.count() == 2, "the open list must not be rebuilt underneath the user"

    combo.hidePopup()
    qapp.processEvents()
    assert combo.count() == 3, "and the deferred refresh applies once it closes"
    assert window.tf_model.current_model() == "x"


# --------------------------------------------------------------------------------------
# A widget that cannot show a value must not overwrite it
# --------------------------------------------------------------------------------------


def test_a_stored_value_a_combo_cannot_show_survives(qapp, window):
    """Index 0 is the default in every one of these combos.

    So falling back there turned "this value is not in the list" into "the user chose the
    default", and the next autosave persisted it. The same shape replaced a configured
    model with the newest one and a stored microphone with the system default; this is the
    version of it that resets the pasting option.
    """
    settings = Settings()
    settings.output.paste_mode = "after_transcription"
    settings.theme = "some_future_theme"
    settings.transcription.delay = "some_future_delay"
    window.load(settings)
    qapp.processEvents()

    collected = window.collect()
    assert collected.output.paste_mode == "after_transcription", "a valid value is kept"
    assert collected.theme == "some_future_theme", (
        "an unrecognised theme must not become 'blue'"
    )
    assert collected.transcription.delay == "some_future_delay", (
        "an unrecognised delay must not become 'minimal'"
    )


def test_a_deliberate_choice_still_wins_over_the_stored_value(qapp, window):
    """The fix must not go so far that changing a setting stops working."""
    settings = Settings()
    settings.output.paste_mode = "some_future_mode"
    window.load(settings)
    qapp.processEvents()

    index = window.paste_mode.findData("after_transcription")
    assert index >= 0
    window.paste_mode.setCurrentIndex(index)
    window.paste_mode.activated.emit(index)  # only a user selection emits this
    qapp.processEvents()

    assert window.collect().output.paste_mode == "after_transcription"


def test_a_normal_change_is_still_collected(qapp, window):
    settings = Settings()
    settings.output.paste_mode = "off"
    window.load(settings)
    index = window.paste_mode.findData("after_transformation")
    window.paste_mode.setCurrentIndex(index)
    qapp.processEvents()
    assert window.collect().output.paste_mode == "after_transformation"


def test_the_text_scale_fallback_is_not_a_different_default(qapp, window):
    """It used to be 2.0 while the app's own default is 1.6.

    So a combo that could not show the stored scale did not merely reset it -- it jumped
    the user to Largest, a value nothing else in the app considers the default.
    """
    settings = Settings()
    settings.text_scale = 1.3
    window.load(settings)
    qapp.processEvents()
    assert window.collect().text_scale == pytest.approx(1.3)

    settings.text_scale = 1.55  # not one of TEXT_SCALES
    window.load(settings)
    qapp.processEvents()
    assert window.collect().text_scale == pytest.approx(1.55), (
        "an unrecognised scale must be preserved, not replaced with 2.0"
    )


# -- the microphone choice is never replaced by "System default" -----------------------


def _with_devices(monkeypatch, names, *, platform="linux"):
    import yada.ui.settings_window as sw
    from yada.audio import DeviceInfo

    devices = [
        DeviceInfo(index=i, name=n, channels=1, default_samplerate=48000.0, hostapi="ALSA")
        for i, n in enumerate(names)
    ]
    monkeypatch.setattr(sw.sys, "platform", platform)
    monkeypatch.setattr(sw, "list_input_devices", lambda: devices)


def _stored(device):
    s = Settings()
    s.audio.device = device
    return s


def test_an_absent_microphone_is_not_saved_as_system_default(window, monkeypatch):
    _with_devices(monkeypatch, ["Other Mic (hw:1,0)"])
    window.load(_stored("Webcam: USB Audio (hw:3,0)"))
    assert window.audio_device.currentData() == "Webcam: USB Audio (hw:3,0)"
    assert "not connected" in window.audio_device.currentText()
    assert window.collect().audio.device == "Webcam: USB Audio (hw:3,0)"
    # An unrelated change autosaves the whole form; the microphone must survive it.
    window.always_copy.setChecked(not window.always_copy.isChecked())
    assert _commit(window).audio.device == "Webcam: USB Audio (hw:3,0)"


def test_a_renumbered_microphone_stays_selected(window, monkeypatch):
    _with_devices(monkeypatch, ["Other Mic (hw:3,0)", "Webcam: USB Audio (hw:5,0)"])
    window.load(_stored("Webcam: USB Audio (hw:3,0)"))
    assert window.collect().audio.device == "Webcam: USB Audio (hw:5,0)"


def test_rescanning_keeps_an_absent_microphone(window, monkeypatch):
    _with_devices(monkeypatch, ["Webcam: USB Audio (hw:3,0)"])
    window.load(_stored("Webcam: USB Audio (hw:3,0)"))
    _with_devices(monkeypatch, [])
    window._reload_devices()
    assert window.collect().audio.device == "Webcam: USB Audio (hw:3,0)"


def test_system_default_is_still_a_choice(window, monkeypatch):
    _with_devices(monkeypatch, ["Webcam: USB Audio (hw:3,0)"])
    window.load(_stored("Webcam: USB Audio (hw:3,0)"))
    window.audio_device.setCurrentIndex(0)
    assert window.collect().audio.device is None


def test_windows_picker_reuses_the_real_endpoint_for_a_saved_mme_name(window, monkeypatch):
    name = "Microphone Array (Intel® Smart Sound Technology for Digital Microphones)"
    _with_devices(monkeypatch, [name], platform="win32")
    window.load(_stored(name[:31]))
    assert window.audio_device.count() == 2
    assert window.audio_device.currentData() == name


def test_windows_picker_preserves_a_missing_selection_without_listing_it(window, monkeypatch):
    _with_devices(monkeypatch, ["Microphone Array"], platform="win32")
    saved = "Microphone Array 2 ()"
    window.load(_stored(saved))
    window._reload_devices()
    assert window.audio_device.count() == 2
    assert window.audio_device.currentIndex() == -1
    assert window.collect().audio.device == saved

    window.audio_device.setCurrentIndex(0)
    window.audio_device.activated.emit(0)
    assert window.collect().audio.device is None


def test_no_windows_advice_on_other_platforms(window):
    import sys

    from PySide6.QtWidgets import QLabel

    if sys.platform == "win32":
        pytest.skip("Windows text belongs on Windows")
    texts = [label.text() for label in window.findChildren(QLabel)]
    texts += [w.toolTip() for w in window.findChildren(QLabel)]
    assert not [t for t in texts if "Windows" in t]


def test_a_refused_mic_test_does_not_leave_its_reason_behind(window):
    help_text = window.mic_test_note.text()
    refuse = lambda active: active and window.stop_mic_test("Not while a dictation is running.")  # noqa: E731
    window.mic_test_requested.connect(refuse)
    window.mic_test.setChecked(True)
    assert window.mic_test_note.text() == "Not while a dictation is running."
    assert not window.mic_test.isChecked()
    # The dictation ends, which is what the app reports through this call.
    window.reset_mic_test_note()
    assert window.mic_test_note.text() == help_text
    # And a later test that is allowed to start clears a stale reason by itself.
    window.stop_mic_test("Not while a dictation is running.")
    window.mic_test_requested.disconnect(refuse)
    window.mic_test.setChecked(True)
    assert window.mic_test_note.text() == help_text
    window.mic_test.setChecked(False)
