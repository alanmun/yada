"""Holding the shortcut retries the last recording instead of listening again.

The constraint that shapes this: Win32's RegisterHotKey reports presses and never releases,
so a hold can only be found by asking the keyboard afterwards. The XDG portal does report
the release, and a desktop-bound command reports neither -- hence `yada retry`.
"""

from __future__ import annotations

import pytest

from yada.hotkey.base import HOLD_SECONDS, Combo
from yada.hotkey.external import ExternalHotkeyBackend


def test_the_threshold_is_deliberate():
    """Long enough not to be reached by accident, short enough to hold on purpose."""
    assert 1.5 <= HOLD_SECONDS <= 5.0


def test_the_keys_to_watch_cover_both_sides_of_the_keyboard():
    """A modifier held on the right must count as held."""
    groups = Combo.parse("ctrl+shift+;").win32_hold_keys()
    assert groups == ((0x11,), (0x10,), (0xBA,)), "VK_CONTROL and VK_SHIFT are side-agnostic"

    # The Windows key has no combined virtual key, so both must be listed.
    assert (0x5B, 0x5C) in Combo.parse("meta+space").win32_hold_keys()


def test_every_key_of_the_combo_is_watched():
    """Releasing any one of them ends the hold."""
    groups = Combo.parse("ctrl+alt+shift+k").win32_hold_keys()
    assert len(groups) == 4
    assert (0x4B,) in groups  # the K itself, not just the modifiers


# --------------------------------------------------------------------------------------
# Win32 polling
# --------------------------------------------------------------------------------------


class FakeUser32:
    """Answers GetAsyncKeyState from a script of "is the combo still down" answers."""

    def __init__(self, answers: list[bool]) -> None:
        self._answers = list(answers)
        self.polls = 0

    def GetAsyncKeyState(self, _vk):
        # One answer per poll of the whole combo; the backend asks per key, so hold the
        # answer steady until it has asked about all of them.
        if not self._answers:
            return 0
        return 0x8000 if self._answers[0] else 0

    def advance(self) -> None:
        self.polls += 1
        if self._answers:
            self._answers.pop(0)


@pytest.fixture
def backend(monkeypatch):
    pytest.importorskip("yada.hotkey.win32")
    from yada.hotkey import win32

    monkeypatch.setattr(win32, "HOLD_SECONDS", 0.2)
    instance = win32.Win32HotkeyBackend()
    instance._combo = Combo.parse("ctrl+shift+;")
    return instance


def test_a_release_reads_as_an_ordinary_press(backend):
    """The common case: tap, and the recording starts."""
    user32 = FakeUser32([False])
    assert backend._wait_for_hold(user32) is False


def test_keys_still_down_at_the_threshold_read_as_a_hold(backend):
    user32 = FakeUser32([True] * 200)
    assert backend._wait_for_hold(user32) is True


def test_a_release_partway_through_is_not_a_hold(backend, monkeypatch):
    """Someone who holds for a moment and lets go wanted to dictate, not to retry."""
    from yada.hotkey import win32

    monkeypatch.setattr(win32, "HOLD_SECONDS", 5.0)
    state = {"down": True}

    class Releasing:
        def __init__(self):
            self.polls = 0

        def GetAsyncKeyState(self, _vk):
            self.polls += 1
            # Down for the first few polls of the combo, then released.
            if self.polls > 9:
                state["down"] = False
            return 0x8000 if state["down"] else 0

    assert backend._wait_for_hold(Releasing()) is False


def test_shutting_down_abandons_the_wait(backend):
    """Otherwise stop() would block for the whole threshold."""
    backend._stop.set()
    assert backend._wait_for_hold(FakeUser32([True] * 200)) is False


# --------------------------------------------------------------------------------------
# The backend that cannot tell
# --------------------------------------------------------------------------------------


def test_the_external_backend_accepts_a_hold_callback_and_ignores_it():
    """A desktop-bound command reports that the shortcut fired, never for how long.

    Accepting the argument keeps one protocol for all three backends; `yada retry` is the
    equivalent gesture there.
    """
    calls = []
    backend = ExternalHotkeyBackend()
    backend.start(
        Combo.parse("ctrl+shift+;"), lambda: calls.append("toggle"), lambda: calls.append("hold")
    )
    assert calls == [], "nothing fires until the desktop invokes the command"
    assert backend.problem() is None
