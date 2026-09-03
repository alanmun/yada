"""Does the app actually connect the signals it emits?

Most of yada's worst bugs have been wiring, invisible to every unit test: delivery called
on the asyncio thread, state pushed into a window before it was shown, and a live transcript
emitted on every delta with *nothing connected to it* -- so "transcribe while I speak" had
no visible effect and looked exactly like streaming being broken.

`YadaApp.__init__` starts no IPC, registers no hotkey and reaches no network, so it can be
built in a test. This is the smallest useful version of an app-level harness.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from yada.pipeline.session import SessionResult, SessionState


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def yada(qapp, tmp_path, monkeypatch):
    """A real YadaApp, pointed at a throwaway config and cache."""
    import yada.config as cfg
    from yada.app import YadaApp

    monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr("yada.providers.catalog.cache_dir", lambda: tmp_path)
    app = YadaApp(qapp)
    yield app
    # Torn down explicitly. Leaving Qt objects to be collected at interpreter exit -- after
    # the QApplication is gone -- crashes in shiboken rather than failing a test, which is
    # a miserable thing to debug and worse to hit intermittently in CI.
    app.overlay.dismiss()
    app.overlay.deleteLater()
    app.tray.hide()
    app.async_thread.stop()
    qapp.processEvents()


def test_a_live_partial_reaches_the_overlay(yada, qapp):
    """The bug: partials were emitted into a signal with no receivers."""
    yada.bridge.on_partial("hello there")
    qapp.processEvents()
    assert "hello there" in yada.overlay.text.text()
    assert "live" in yada.overlay.status.text().lower(), (
        "arriving deltas are what distinguishes live from uploaded"
    )


def test_recording_shows_the_overlay_and_clears_the_last_problem(yada, qapp):
    yada.tray.set_problem("something from last time")
    yada.bridge.on_state(SessionState.RECORDING)
    qapp.processEvents()
    assert yada.overlay.status.text() == "Listening…"
    assert yada.tray._problem is None, "a new dictation starts from a clean slate"


def test_a_warning_reaches_the_tray_tooltip(yada, qapp):
    """Notifications are off by default on Windows, so the toast cannot be the only channel.

    For a release it was: `_on_warning` called `notify()` and nothing else, so a degraded
    transcription explained itself to nobody.
    """
    yada.bridge.on_warning("Live transcription unavailable, using the recording instead")
    qapp.processEvents()
    assert "Live transcription unavailable" in (yada.tray._problem or "")
    assert "Live transcription unavailable" in yada.overlay.text.text()


def _finished(**overrides):
    base = {
        "transcript": "some words",
        "final_text": "some words",
        "duration_seconds": 1.0,
        "streamed": True,
        "transform": None,
        "warnings": [],
    }
    return SessionResult(**{**base, **overrides})


def test_a_clean_finish_removes_the_panel(yada, qapp):
    """The transcription chime already confirmed it finished.

    A "Done" line afterwards confirms something the user has just heard, and then sits on
    screen outstaying its welcome.
    """
    yada.bridge.on_state(SessionState.RECORDING)
    yada.bridge.on_partial("some words")
    qapp.processEvents()
    assert yada.overlay.isVisible()

    yada.bridge.on_finished(_finished())
    qapp.processEvents()
    assert not yada.overlay.isVisible(), "nothing to say means nothing on screen"


def test_a_finish_with_a_warning_keeps_the_panel_up(yada, qapp):
    """A warning has to actually be read, so this one does not vanish."""
    yada.bridge.on_finished(_finished(warnings=["Live transcription was unavailable"]))
    qapp.processEvents()
    assert yada.overlay.isVisible()
    assert "unavailable" in yada.overlay.status.text()
    assert "unavailable" in (yada.tray._problem or "")


def test_whether_it_was_live_is_still_recorded_on_the_tray(yada, qapp):
    """Removing the panel's "Done — live" must not lose the answer entirely."""
    for streamed, expected in ((True, "streamed"), (False, "uploaded")):
        yada.bridge.on_finished(_finished(streamed=streamed))
        qapp.processEvents()
        assert expected in yada.tray._status_line, (
            f"streamed={streamed} should still be reported as {expected}"
        )


def test_an_error_is_reported_rather_than_swallowed(yada, qapp):
    yada.bridge.on_error("Transcription produced no text. The live connection failed.")
    qapp.processEvents()
    assert "produced no text" in (yada.tray._problem or "")


def test_turning_the_overlay_off_keeps_it_off(yada, qapp):
    """The setting has to reach the widget, not just the settings file."""
    yada.settings.output.show_overlay = False
    yada._apply_notification_setting()

    yada.bridge.on_state(SessionState.RECORDING)
    yada.bridge.on_partial("some words")
    qapp.processEvents()
    assert not yada.overlay.isVisible()

    yada.settings.output.show_overlay = True
    yada._apply_notification_setting()
    yada.bridge.on_partial("some words")
    qapp.processEvents()
    assert yada.overlay.isVisible()


def test_notifications_off_does_not_silence_the_tooltip(yada, qapp):
    """The two channels are independent, which is the point of the fix."""
    yada.settings.output.show_notifications = False
    yada._apply_notification_setting()
    assert yada.tray.notifications_enabled is False

    yada.bridge.on_warning("something went wrong")
    qapp.processEvents()
    assert "something went wrong" in (yada.tray._problem or "")
