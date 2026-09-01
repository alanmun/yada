"""Global hotkey delivery, per platform.

Three backends, one interface. Selection is automatic but overridable, because the right
answer depends on the session rather than the OS:

* **win32** -- RegisterHotKey. Reliable, no special permissions.
* **kde_portal** -- the XDG GlobalShortcuts portal. The sanctioned Wayland route; the
  compositor owns the binding and the user approves it once.
* **external** -- the desktop environment owns the binding and invokes `yada toggle`.
  Always available, since the trigger arrives over IPC rather than from a key grab.

The app wires the IPC "toggle" command to the same handler as the backend callback, so the
external path is not a special case anywhere above this package.
"""

from __future__ import annotations

import asyncio
import sys

from .base import Combo, HotkeyBackend, InvalidCombo, TriggerCallback
from .external import ExternalHotkeyBackend, toggle_command
from .kde_portal import KdePortalHotkeyBackend, wayland_session

__all__ = [
    "Combo",
    "ExternalHotkeyBackend",
    "HotkeyBackend",
    "InvalidCombo",
    "KdePortalHotkeyBackend",
    "TriggerCallback",
    "available_backends",
    "create_backend",
    "toggle_command",
    "wayland_session",
]


def available_backends() -> list[str]:
    names = []
    if sys.platform == "win32":
        from .win32 import Win32HotkeyBackend

        if Win32HotkeyBackend.available():
            names.append("win32")
    if KdePortalHotkeyBackend.available():
        names.append("kde_portal")
    names.append("external")
    return names


def create_backend(
    preference: str = "auto", *, loop: asyncio.AbstractEventLoop | None = None
) -> HotkeyBackend:
    """Build a backend. `preference` is 'auto', or a name from available_backends().

    'auto' picks win32 on Windows, the portal on a Wayland session, and otherwise external.
    An explicitly requested backend that is unavailable falls back to external rather than
    raising -- a missing portal should cost the user a settings hint, not a crash on launch.
    """
    if preference == "auto":
        if sys.platform == "win32":
            preference = "win32"
        elif wayland_session() and KdePortalHotkeyBackend.available() and loop is not None:
            preference = "kde_portal"
        else:
            # X11 could grab keys directly, but the DE-bound command works identically and
            # is one less code path to maintain. Revisit if X11 users want in-app rebinding.
            preference = "external"

    if preference == "win32" and sys.platform == "win32":
        from .win32 import Win32HotkeyBackend

        return Win32HotkeyBackend()
    if preference == "kde_portal" and loop is not None and KdePortalHotkeyBackend.available():
        return KdePortalHotkeyBackend(loop)
    return ExternalHotkeyBackend()
