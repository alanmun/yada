"""Keystroke injection for auto-paste, where the platform allows it.

The asymmetry here is the whole story. On Windows, synthesising Ctrl+V is a supported API
call. On Wayland it is deliberately impossible for an ordinary client -- that is a security
property of the protocol, not an oversight -- so it requires `ydotool`, which injects at the
kernel level through `/dev/uinput`.

Therefore auto-paste is a *detected capability*, never an assumed one. The text is always
placed on the clipboard first, so the worst case degrades to "press Ctrl+V yourself" rather
than to losing the dictation. The settings pane says which state you are in before you turn
the option on, rather than failing silently at paste time.

Not implemented: the `RemoteDesktop` portal, which can also inject input on KDE. It needs a
per-session consent dialog, which is unacceptable for an action meant to be invisible.
ydotool asks once, at install time, and then works.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from typing import Protocol, runtime_checkable

from .portal_paste import PortalPasteBackend

# Linux input event codes, from <linux/input-event-codes.h>. ydotool speaks these directly,
# which is more stable across versions than its named-key syntax.
KEY_LEFTCTRL = 29
KEY_V = 47

YDOTOOL_INSTALL_HINT = (
    "Install ydotool and enable its daemon:\n"
    "    sudo apt install ydotool     # or your distro's package\n"
    "    sudo systemctl enable --now ydotoold\n"
    "    sudo usermod -aG input $USER   # then log out and back in"
)


@runtime_checkable
class PasteBackend(Protocol):
    name: str

    @staticmethod
    def available() -> bool: ...

    def paste(self) -> tuple[bool, str | None]:
        """Send the paste keystroke. Returns (succeeded, error message)."""
        ...

    def describe(self) -> str:
        """User-facing explanation of what will happen, shown next to the setting."""
        ...


def _win32_input():
    """Bind SendInput with the *complete* INPUT structure.

    The size of INPUT is set by its largest union member, MOUSEINPUT -- and the union here
    used to contain only KEYBDINPUT. That made the structure 32 bytes where Windows requires
    40, and `cbSize` not matching is a documented hard failure: SendInput returned 0 with
    ERROR_INVALID_PARAMETER and injected nothing. Auto-paste never worked on Windows, on any
    release, and said "error 0" while failing because `ctypes.windll` does not set
    `use_last_error`.

    So: every union member is declared, `dwExtraInfo` is ULONG_PTR rather than a pointer
    type, and the DLL is bound with `use_last_error=True`. `tests/test_paste.py` asserts the
    size, because nothing else about the failure is visible.
    """
    import ctypes
    from ctypes import wintypes

    ULONG_PTR = wintypes.WPARAM

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    def keyboard_event(vk: int, *, up: bool) -> INPUT:
        event = INPUT()
        event.type = INPUT_KEYBOARD
        event.ki.wVk = vk
        event.ki.wScan = 0
        event.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
        event.ki.time = 0
        event.ki.dwExtraInfo = 0
        return event

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.c_void_p, ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    return user32, INPUT, keyboard_event


class Win32PasteBackend:
    name = "win32"

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def paste(self) -> tuple[bool, str | None]:
        import ctypes

        user32, INPUT, keyboard_event = _win32_input()
        VK_CONTROL, VK_V = 0x11, 0x56
        sequence = (INPUT * 4)(
            keyboard_event(VK_CONTROL, up=False),
            keyboard_event(VK_V, up=False),
            keyboard_event(VK_V, up=True),
            keyboard_event(VK_CONTROL, up=True),
        )
        ctypes.set_last_error(0)
        sent = user32.SendInput(4, ctypes.byref(sequence), ctypes.sizeof(INPUT))
        if sent != 4:
            code = ctypes.get_last_error()
            if code == 87:  # ERROR_INVALID_PARAMETER
                return False, (
                    "SendInput rejected the input structure (error 87). This is a bug in "
                    "yada, not a problem with your system."
                )
            # Otherwise the usual cause is UIPI: a normal-privilege app cannot inject into
            # an elevated window. Worth saying so plainly rather than a raw code.
            return False, (
                f"SendInput was blocked (error {code}). The focused window may be running "
                "as administrator, which blocks input from non-elevated apps."
            )
        return True, None

    def describe(self) -> str:
        return "Pastes by sending Ctrl+V to the focused window."


class X11PasteBackend:
    """Pastes on X11 using the XTEST extension.

    No daemon, no extra permissions, no package to install: XTEST ships with every X
    server and is how `xdotool` does the same job. Its absence here was a straightforward
    gap rather than a platform limitation -- yada told X11 users that pasting "requires
    ydotool", which was true of yada and not of their system.

    Deliberately not used on Wayland even when an X server is reachable. Under XWayland,
    XTEST reaches X11 clients only, so a paste aimed at a native Wayland window either
    lands somewhere unintended or nowhere at all -- which is worse than saying no.
    """

    name = "xtest"

    @staticmethod
    def available() -> bool:
        if sys.platform == "win32" or not os.environ.get("DISPLAY"):
            return False
        # A Wayland session is Wayland's to handle, even though XWayland answers here.
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
            return False
        return _x11_libraries() is not None

    def paste(self) -> tuple[bool, str | None]:
        libs = _x11_libraries()
        if libs is None:
            return False, "X11 libraries are not available."
        xlib, xtst = libs
        display = xlib.XOpenDisplay(None)
        if not display:
            return False, "Could not open the X display."
        try:
            major = ctypes.c_int()
            minor = ctypes.c_int()
            event = ctypes.c_int()
            error = ctypes.c_int()
            if not xtst.XTestQueryExtension(
                display,
                ctypes.byref(event),
                ctypes.byref(error),
                ctypes.byref(major),
                ctypes.byref(minor),
            ):
                return False, "This X server does not offer the XTEST extension."

            control = xlib.XKeysymToKeycode(display, xlib.XStringToKeysym(b"Control_L"))
            v = xlib.XKeysymToKeycode(display, xlib.XStringToKeysym(b"v"))
            if not control or not v:
                return False, "Could not map Ctrl+V on this keyboard layout."

            for keycode, press in ((control, 1), (v, 1), (v, 0), (control, 0)):
                xtst.XTestFakeKeyEvent(display, keycode, press, 0)
            xlib.XFlush(display)
        finally:
            xlib.XCloseDisplay(display)
        return True, None

    def describe(self) -> str:
        return "Pastes by sending Ctrl+V to the focused window, using X11's XTEST extension."


def _x11_libraries():
    """(libX11, libXtst) with their signatures bound, or None if either is missing.

    Bound rather than left to ctypes' defaults because `XOpenDisplay` returns a pointer:
    the default int return truncates it on 64-bit, which fails in ways that look like the
    display being unavailable.
    """
    import ctypes.util

    x11_path = ctypes.util.find_library("X11")
    xtst_path = ctypes.util.find_library("Xtst")
    if not x11_path or not xtst_path:
        return None
    try:
        xlib = ctypes.CDLL(x11_path)
        xtst = ctypes.CDLL(xtst_path)
    except OSError:
        return None

    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xlib.XFlush.argtypes = [ctypes.c_void_p]
    xlib.XStringToKeysym.restype = ctypes.c_ulong
    xlib.XStringToKeysym.argtypes = [ctypes.c_char_p]
    xlib.XKeysymToKeycode.restype = ctypes.c_ubyte
    xlib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    xtst.XTestFakeKeyEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    xtst.XTestQueryExtension.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_int)] * 4
    return xlib, xtst


class YdotoolPasteBackend:
    name = "ydotool"

    @staticmethod
    def available() -> bool:
        return sys.platform != "win32" and shutil.which("ydotool") is not None

    def paste(self) -> tuple[bool, str | None]:
        cmd = [
            "ydotool",
            "key",
            f"{KEY_LEFTCTRL}:1",
            f"{KEY_V}:1",
            f"{KEY_V}:0",
            f"{KEY_LEFTCTRL}:0",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=3.0, check=False)
        except FileNotFoundError:
            return False, "ydotool is not installed."
        except subprocess.TimeoutExpired:
            return False, "ydotool did not respond."
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            # By far the most common failure: the daemon is not running or the user is not
            # in the input group. Say which, instead of echoing a socket error.
            return False, f"ydotool failed: {detail or 'is ydotoold running?'}"
        return True, None

    def describe(self) -> str:
        return "Pastes via ydotool, which injects Ctrl+V at the kernel level."


class NoPasteBackend:
    """Clipboard-only. Not an error state -- just the honest state on stock Wayland."""

    name = "none"

    @staticmethod
    def available() -> bool:
        return True

    def paste(self) -> tuple[bool, str | None]:
        return False, "Automatic pasting is not available in this session."

    def describe(self) -> str:
        if sys.platform == "win32":
            return "Automatic pasting is unavailable."
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
            return (
                "Wayland deliberately does not let one application press keys in another. "
                "yada normally asks the desktop's RemoteDesktop portal to do it instead, "
                "but this session does not offer one, so text is copied to the clipboard "
                "and you paste it with Ctrl+V.\n\n"
                "To enable automatic pasting anyway:\n" + YDOTOOL_INSTALL_HINT
            )
        return (
            "No way to send a keystroke was found in this session, so text is copied to "
            "the clipboard and you paste it with Ctrl+V. On X11 yada uses the XTEST "
            "extension, which needs nothing installed; this session appears to have "
            "neither an X display nor ydotool.\n\n"
            "To enable automatic pasting:\n" + YDOTOOL_INSTALL_HINT
        )


def create_paste_backend() -> PasteBackend:
    """Best available backend. Never returns None; NoPasteBackend is the honest floor."""
    # Ordered by how little each asks of the user. XTEST and the portal both need nothing
    # installed; the portal is preferred over ydotool because a one-off approval dialog is
    # less to ask than a daemon, a udev group and a reboot -- and because ydotool being on
    # PATH says nothing about whether ydotoold is actually running.
    for backend in (
        Win32PasteBackend,
        X11PasteBackend,
        PortalPasteBackend,
        YdotoolPasteBackend,
    ):
        if backend.available():
            return backend()
    return NoPasteBackend()


def paste_available() -> bool:
    return not isinstance(create_paste_backend(), NoPasteBackend)
