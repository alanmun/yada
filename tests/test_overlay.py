"""The live transcript panel.

It exists because "transcribe while I speak" previously had no visible effect at all: the
session emitted a partial on every delta and nothing was connected to it.

Both of its hard constraints are about staying out of the way, and both are asserted here,
because getting either wrong breaks the app rather than the panel: yada pastes into whatever
window you were using, so taking focus would break pasting, and being clickable would put a
box over whatever you were about to click.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from yada.ui.overlay import MAX_CHARS, LiveOverlay, _tail


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def overlay(qapp):
    panel = LiveOverlay()
    yield panel
    panel.dismiss()


def test_it_never_takes_focus(overlay):
    """Activating itself would steal focus from the window being dictated into."""
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert overlay.windowFlags() & Qt.WindowType.Tool


def test_clicks_pass_straight_through_it(overlay):
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def test_it_stays_above_the_window_being_dictated_into(overlay):
    assert overlay.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert overlay.windowFlags() & Qt.WindowType.FramelessWindowHint


def test_listening_becomes_live_when_a_delta_arrives(overlay, qapp):
    overlay.begin()
    qapp.processEvents()
    assert overlay.status.text() == "Listening…"
    assert overlay.text.text() == ""

    overlay.set_partial("the quick brown fox")
    qapp.processEvents()
    assert "live" in overlay.status.text().lower()
    assert overlay.text.text() == "the quick brown fox"


def test_a_dictation_that_never_streams_never_claims_to(overlay, qapp):
    """No deltas means the status stays "Listening", which is the honest answer."""
    overlay.begin()
    overlay.set_status("Finishing…")
    qapp.processEvents()
    assert "live" not in overlay.status.text().lower()


def test_the_finished_status_is_whatever_the_caller_says(overlay, qapp):
    overlay.finish("some words", status="Done — uploaded on stop")
    qapp.processEvents()
    assert overlay.status.text() == "Done — uploaded on stop"
    assert overlay.text.text() == "some words"


def test_a_problem_is_shown_even_with_no_dictation_running(overlay, qapp):
    overlay.report("Live transcription unavailable, using the recording instead")
    qapp.processEvents()
    assert overlay.isVisible()
    assert "unavailable" in overlay.text.text()


def test_disabling_it_hides_it_and_keeps_it_hidden(overlay, qapp):
    overlay.begin()
    qapp.processEvents()
    assert overlay.isVisible()

    overlay.set_enabled(False)
    qapp.processEvents()
    assert not overlay.isVisible()

    overlay.begin()
    overlay.set_partial("still nothing")
    qapp.processEvents()
    assert not overlay.isVisible(), "an overlay turned off must stay off"


def test_long_text_keeps_the_most_recent_words():
    """A live transcript is written at the end, so that is the end to keep."""
    tail = _tail("start " + "x" * 500 + " finish")
    assert tail.endswith("finish")
    assert tail.startswith("…")
    assert len(tail) <= MAX_CHARS + 1


def test_whitespace_is_collapsed():
    assert _tail("  two   words \n here ") == "two words here"


def test_empty_text_is_harmless():
    assert _tail("") == ""
    assert _tail(None) == ""
