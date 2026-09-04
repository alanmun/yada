"""Which paste backend a session gets, and why.

yada shipped with only a Windows backend and ydotool, so it told X11 users that automatic
pasting "requires ydotool". That is true of yada and not of X11: XTEST is a core extension,
present on every X server, and is how xdotool does the same job with nothing installed.
"""

from __future__ import annotations

import sys

import pytest

from yada.output import paste as paste_mod
from yada.output.paste import (
    NoPasteBackend,
    Win32PasteBackend,
    X11PasteBackend,
    create_paste_backend,
)


@pytest.fixture
def session(monkeypatch):
    """A clean environment to describe a session into."""

    def configure(*, platform="linux", display=None, wayland=None, session_type=None, ydotool=None):
        monkeypatch.setattr(paste_mod.sys, "platform", platform)
        for name, value in (
            ("DISPLAY", display),
            ("WAYLAND_DISPLAY", wayland),
            ("XDG_SESSION_TYPE", session_type),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        monkeypatch.setattr(paste_mod.shutil, "which", lambda _n: ydotool)

    return configure


def test_x11_is_used_without_anything_installed(session, monkeypatch):
    """The whole point: an X11 user should never be told to install a daemon."""
    session(display=":0", session_type="x11", ydotool=None)
    monkeypatch.setattr(paste_mod, "_x11_libraries", lambda: ("xlib", "xtst"))
    assert X11PasteBackend.available() is True
    assert create_paste_backend().name == "xtest"


def test_x11_is_preferred_over_ydotool(session, monkeypatch):
    """XTEST needs no daemon, so it wins even where both would work."""
    session(display=":0", session_type="x11", ydotool="/usr/bin/ydotool")
    monkeypatch.setattr(paste_mod, "_x11_libraries", lambda: ("xlib", "xtst"))
    assert create_paste_backend().name == "xtest"


def test_wayland_does_not_use_xtest(session, monkeypatch):
    """Under XWayland, XTEST reaches X11 clients only.

    A paste aimed at a native Wayland window would land somewhere unintended or nowhere,
    which is worse than declining.
    """
    monkeypatch.setattr(paste_mod, "_x11_libraries", lambda: ("xlib", "xtst"))
    session(display=":0", wayland="wayland-0", session_type="wayland", ydotool=None)
    assert X11PasteBackend.available() is False
    assert create_paste_backend().name == "none"

    # ...but ydotool works there, because it injects below the compositor.
    session(display=":0", wayland="wayland-0", session_type="wayland", ydotool="/usr/bin/ydotool")
    assert create_paste_backend().name == "ydotool"


def test_no_display_means_no_x11_backend(session, monkeypatch):
    monkeypatch.setattr(paste_mod, "_x11_libraries", lambda: ("xlib", "xtst"))
    session(display=None, ydotool=None)
    assert X11PasteBackend.available() is False
    assert create_paste_backend().name == "none"


def test_missing_x_libraries_are_not_a_crash(session, monkeypatch):
    session(display=":0", session_type="x11", ydotool=None)
    monkeypatch.setattr(paste_mod, "_x11_libraries", lambda: None)
    assert X11PasteBackend.available() is False
    ok, error = X11PasteBackend().paste()
    assert ok is False and "X11 libraries" in (error or "")


def test_windows_still_wins_on_windows(session):
    session(platform="win32", display=":0", ydotool="/usr/bin/ydotool")
    assert create_paste_backend().name == "win32"
    assert Win32PasteBackend.available() is True


def test_the_wayland_explanation_does_not_blame_yada(session):
    """It is a deliberate Wayland design decision, and the text should say so."""
    session(wayland="wayland-0", session_type="wayland", ydotool=None)
    text = NoPasteBackend().describe()
    assert "Wayland deliberately" in text
    assert "ydotool" in text


def test_a_bare_session_is_not_told_wayland_is_the_reason(session):
    """Saying "Wayland does not allow it" to someone not running Wayland is just wrong."""
    session(display=None, ydotool=None)
    text = NoPasteBackend().describe()
    assert "Wayland deliberately" not in text
    assert "XTEST" in text


@pytest.mark.skipif(sys.platform == "win32", reason="XTEST is not a Windows thing")
def test_the_x11_bindings_match_the_libraries():
    """Guards the class of bug that made SendInput fail silently on Windows for 15 releases."""
    libs = paste_mod._x11_libraries()
    if libs is None:
        pytest.skip("no X11 libraries in this environment")
    xlib, _xtst = libs
    import ctypes

    assert xlib.XOpenDisplay.restype is ctypes.c_void_p, (
        "a pointer return left as the default int truncates on 64-bit"
    )
    assert xlib.XKeysymToKeycode.restype is ctypes.c_ubyte
