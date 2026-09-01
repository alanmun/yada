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

import shutil
import subprocess
import sys
from typing import Protocol, runtime_checkable

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


class Win32PasteBackend:
    name = "win32"

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def paste(self) -> tuple[bool, str | None]:
        import ctypes
        from ctypes import wintypes

        # SendInput requires the full INPUT structure; there is no simpler supported call.
        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        VK_CONTROL, VK_V = 0x11, 0x56

        def event(vk: int, up: bool) -> INPUT:
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki = KEYBDINPUT(
                wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=None
            )
            return inp

        sequence = (INPUT * 4)(
            event(VK_CONTROL, False),
            event(VK_V, False),
            event(VK_V, True),
            event(VK_CONTROL, True),
        )
        sent = ctypes.windll.user32.SendInput(4, sequence, ctypes.sizeof(INPUT))
        if sent != 4:
            code = ctypes.get_last_error()
            # The usual cause is UIPI: a normal-privilege app cannot inject into an
            # elevated window. Worth saying so plainly rather than reporting a raw code.
            return False, (
                f"SendInput was blocked (error {code}). The focused window may be running "
                "as administrator, which blocks input from non-elevated apps."
            )
        return True, None

    def describe(self) -> str:
        return "Pastes by sending Ctrl+V to the focused window."


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
        return (
            "Wayland does not let applications press keys for you, so text is copied to "
            "the clipboard and you paste it with Ctrl+V.\n\n"
            "To enable automatic pasting:\n" + YDOTOOL_INSTALL_HINT
        )


def create_paste_backend() -> PasteBackend:
    """Best available backend. Never returns None; NoPasteBackend is the honest floor."""
    for backend in (Win32PasteBackend, YdotoolPasteBackend):
        if backend.available():
            return backend()
    return NoPasteBackend()


def paste_available() -> bool:
    return not isinstance(create_paste_backend(), NoPasteBackend)
