"""Clipboard writes, routed around Wayland's focus rule.

On Wayland a client may only take the clipboard while one of its surfaces has keyboard
focus. yada lives in the tray and pastes into *other* apps, so it almost never has focus
when a transcript is ready -- and Qt's write was silently dropped. Worse, Qt answers a
read-back from its own copy for a moment, so the old "write, then read back" check passed
and the fallback never ran. The user's previous clipboard (an image, once) got pasted
instead of their words.

So on Wayland the order is reversed:

1. `wl-copy`, which uses the compositor's data-control protocol and needs no focus. The
   result is checked with `wl-paste`, which asks the compositor, not Qt.
2. KDE's Klipper over D-Bus, which owns the clipboard on behalf of the whole session.
3. Qt, last, for desktops with neither.

X11 and Windows have no such rule, and keep Qt as the only path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _qt_copy(text: str) -> tuple[bool, str | None]:
    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError as exc:
        return False, f"Qt unavailable ({exc})"
    app = QGuiApplication.instance()
    if app is None:
        return False, "no Qt application is running"
    clipboard = QGuiApplication.clipboard()
    if clipboard is None:
        return False, "no clipboard available in this session"
    clipboard.setText(text)
    # Read back: on Wayland setText can be accepted and then not take ownership.
    return (clipboard.text() == text), None


def _wl_copy(text: str) -> tuple[bool, str | None]:
    if sys.platform == "win32" or not shutil.which("wl-copy"):
        return False, None
    try:
        # Output discarded, not captured: wl-copy leaves a child serving the clipboard,
        # and that child would hold captured pipes open until the timeout expired.
        proc = subprocess.run(
            ["wl-copy"],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"wl-copy failed ({exc})"
    if proc.returncode != 0:
        return False, f"wl-copy exited with {proc.returncode}"
    return True, None


def _wayland() -> bool:
    return sys.platform != "win32" and (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def _compositor_text() -> str | None:
    """What the compositor holds, or None if that cannot be asked."""
    if not shutil.which("wl-paste"):
        return None
    try:
        proc = subprocess.run(
            ["wl-paste", "--no-newline", "--type", "text"],
            capture_output=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


def _klipper_copy(text: str) -> tuple[bool, str | None]:
    """KDE's clipboard manager sets it for us; it needs no focus of ours."""
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
    except ImportError:
        return False, None
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False, None
    iface = QDBusInterface("org.kde.klipper", "/klipper", "org.kde.klipper.klipper", bus)
    if not iface.isValid():
        return False, None
    reply = iface.call("setClipboardContents", text)
    if reply.type() == QDBusMessage.MessageType.ErrorMessage:
        return False, f"Klipper refused the text ({reply.errorMessage()})"
    return True, None


def copy(text: str) -> tuple[bool, str | None]:
    """Put `text` on the clipboard. Returns (succeeded, error message)."""
    if not text:
        return False, "nothing to copy"
    if not _wayland():
        ok, error = _qt_copy(text)
        return (True, None) if ok else (False, error or "could not write to the clipboard")

    errors: list[str] = []
    for attempt in (_wl_copy, _klipper_copy):
        ok, error = attempt(text)
        if error:
            errors.append(error)
        if not ok:
            continue
        held = _compositor_text()
        if held is None or held == text:
            return True, None
        errors.append(f"{attempt.__name__.strip('_')} did not take")
    # Last resort, and only trustworthy when this app happens to have focus.
    ok, error = _qt_copy(text)
    if ok and _compositor_text() in (None, text):
        return True, None
    return False, "; ".join(e for e in [*errors, error] if e) or (
        "could not write to the clipboard"
    )
