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
    from yada.output.paste import PortalPasteBackend

    monkeypatch.setattr(paste_mod, "_x11_libraries", lambda: ("xlib", "xtst"))
    session(display=":0", wayland="wayland-0", session_type="wayland", ydotool=None)
    assert X11PasteBackend.available() is False

    # With no portal and no ydotool, clipboard-only is the honest floor.
    monkeypatch.setattr(PortalPasteBackend, "available", staticmethod(lambda: False))
    assert create_paste_backend().name == "none"

    # ...ydotool works there, because it injects below the compositor.
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


# --------------------------------------------------------------------------------------
# Wayland, without installing anything
# --------------------------------------------------------------------------------------


def test_wayland_uses_the_portal_before_ydotool(session, monkeypatch):
    """A one-off approval dialog asks less of the user than a daemon and a udev group.

    And `ydotool` being on PATH says nothing about whether `ydotoold` is running, so
    preferring it would sometimes pick a backend that cannot work.
    """
    from yada.output.paste import PortalPasteBackend

    session(display=":0", wayland="wayland-0", session_type="wayland", ydotool="/usr/bin/ydotool")
    monkeypatch.setattr(PortalPasteBackend, "available", staticmethod(lambda: True))
    assert create_paste_backend().name == "portal"


def test_the_portal_is_only_offered_on_wayland(session):
    from yada.output import portal_paste

    session(display=":0", session_type="x11")
    assert portal_paste.PortalPasteBackend.available() is False

    session(display=":0", wayland="wayland-0", session_type="wayland")
    # dbus-fast is a Linux dependency; if it is absent the backend must decline rather
    # than claim a route it cannot take.
    try:
        import dbus_fast  # noqa: F401
    except ImportError:
        assert portal_paste.PortalPasteBackend.available() is False
    else:
        assert portal_paste.PortalPasteBackend.available() is True


def test_the_first_paste_does_not_block_on_a_consent_dialog(session, monkeypatch):
    """Blocking the Qt thread on a modal system dialog would hang the window.

    So the first paste reports that approval is pending and leaves the text on the
    clipboard; the next one works.
    """
    from yada.output import portal_paste

    session(wayland="wayland-0", session_type="wayland")
    backend = portal_paste.PortalPasteBackend()
    started = []
    monkeypatch.setattr(backend, "_begin_session", lambda: started.append(1))

    ok, message = backend.paste()
    assert ok is False
    assert "clipboard" in (message or "").lower()
    assert started == [1], "and it must actually kick the session off"


def test_a_restore_token_is_kept_so_consent_is_asked_once(tmp_path, monkeypatch):
    from yada.output import portal_paste

    monkeypatch.setattr(portal_paste, "config_dir", lambda: tmp_path)
    assert portal_paste._read_restore_token() == ""

    portal_paste._write_restore_token("token-from-the-portal")
    assert portal_paste._read_restore_token() == "token-from-the-portal"

    # An empty token is what the portal returns when it will not persist; do not store it.
    portal_paste._write_restore_token("")
    assert portal_paste._read_restore_token() == "token-from-the-portal"


def test_the_keystroke_is_a_complete_press_and_release(monkeypatch):
    """A held Ctrl left behind would break the user's next keystroke."""
    import asyncio

    from yada.output import portal_paste

    sent: list[tuple[int, int]] = []

    class FakeIface:
        async def call_notify_keyboard_keysym(self, _handle, _options, keysym, state):
            sent.append((keysym, state))

    session = portal_paste._PortalSession()
    session._iface = FakeIface()
    session._handle = "/session"
    session.ready = True

    ok, error = asyncio.run(session.send_paste())
    assert ok is True and error is None
    assert sent == [
        (portal_paste.KEYSYM_CONTROL_L, portal_paste.PRESSED),
        (portal_paste.KEYSYM_V, portal_paste.PRESSED),
        (portal_paste.KEYSYM_V, portal_paste.RELEASED),
        (portal_paste.KEYSYM_CONTROL_L, portal_paste.RELEASED),
    ]


def test_sending_before_the_session_is_ready_is_refused(monkeypatch):
    import asyncio

    from yada.output import portal_paste

    session = portal_paste._PortalSession()
    ok, error = asyncio.run(session.send_paste())
    assert ok is False and "not ready" in (error or "")
