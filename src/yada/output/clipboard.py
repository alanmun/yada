"""Clipboard writes, with a Wayland-specific safety net.

Qt's clipboard is the primary path. On Wayland there is a wrinkle worth guarding against:
clipboard ownership is tied to a surface, and a tray-only application with no visible window
can fail to take ownership. yada stays running, so this is usually fine -- but "usually" is
not good enough for the step that carries the user's words, so `wl-copy` is used as a
fallback when the Qt write does not stick.
"""

from __future__ import annotations

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
        proc = subprocess.run(
            ["wl-copy"], input=text.encode("utf-8"), capture_output=True, timeout=3.0, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"wl-copy failed ({exc})"
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        return False, detail or "wl-copy failed"
    return True, None


def copy(text: str) -> tuple[bool, str | None]:
    """Put `text` on the clipboard. Returns (succeeded, error message)."""
    if not text:
        return False, "nothing to copy"
    ok, error = _qt_copy(text)
    if ok:
        return True, None
    ok, fallback_error = _wl_copy(text)
    if ok:
        return True, None
    return False, error or fallback_error or "could not write to the clipboard"
