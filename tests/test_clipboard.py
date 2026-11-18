"""Which route a clipboard write takes, and how its success is judged.

On Wayland a tray app without keyboard focus cannot take the clipboard through Qt, and Qt
briefly answers a read-back from its own copy -- so the write "succeeded" while the
compositor still held the user's previous clipboard, and that got pasted instead.
"""

from __future__ import annotations

import pytest

from yada.output import clipboard


@pytest.fixture
def routes(monkeypatch):
    """Record which routes are tried; each succeeds unless told otherwise."""
    calls = []
    held = {"text": "previous"}

    def route(name, works=True):
        # "lies" reports success without the compositor ever holding the text.
        def attempt(text):
            calls.append(name)
            if works is True:
                held["text"] = text
            return bool(works), None

        return attempt

    def install(*, wayland=True, wl=True, klipper=True, qt=True, compositor=True):
        monkeypatch.setattr(clipboard, "_wayland", lambda: wayland)
        monkeypatch.setattr(clipboard, "_wl_copy", route("wl-copy", wl))
        monkeypatch.setattr(clipboard, "_klipper_copy", route("klipper", klipper))
        monkeypatch.setattr(clipboard, "_qt_copy", route("qt", qt))
        monkeypatch.setattr(
            clipboard, "_compositor_text", lambda: held["text"] if compositor else None
        )
        return calls

    return install


def test_wayland_never_relies_on_qt_when_wl_copy_works(routes):
    calls = routes()
    assert clipboard.copy("hello") == (True, None)
    assert calls == ["wl-copy"]


def test_wayland_falls_back_to_klipper(routes):
    calls = routes(wl=False)
    assert clipboard.copy("hello") == (True, None)
    assert calls == ["wl-copy", "klipper"]


def test_a_write_the_compositor_does_not_hold_is_not_success(routes):
    """The original bug: Qt 'succeeded' while the old clipboard stayed put."""
    calls = routes(wl=False, klipper=False, qt="lies")
    ok, _error = clipboard.copy("hello")
    assert not ok
    assert calls == ["wl-copy", "klipper", "qt"]


def test_x11_and_windows_keep_qt(routes):
    calls = routes(wayland=False)
    assert clipboard.copy("hello") == (True, None)
    assert calls == ["qt"]
