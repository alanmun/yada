"""No-op backend for when the desktop environment owns the binding.

This is not a degraded mode -- on Wayland it is arguably the most reliable one. The DE does
the key grab it is already designed to do, and invokes `yada toggle`, which reaches the
running app over the local socket in a few milliseconds.

Its job is therefore to tell the user exactly what to bind, since yada cannot do it for them.
"""

from __future__ import annotations

import shutil
import sys

from .base import Combo, TriggerCallback


def toggle_command() -> str:
    """The command to bind in the DE's shortcut settings.

    Prefers the installed launcher on PATH; falls back to the current interpreter so a dev
    checkout gets a command that actually works.
    """
    if found := shutil.which("yada"):
        return f"{found} toggle"
    return f"{sys.executable} -m yada toggle"


class ExternalHotkeyBackend:
    name = "external"

    def __init__(self) -> None:
        self._combo: Combo | None = None

    @staticmethod
    def available() -> bool:
        # Always: it requires nothing of the session, because the trigger arrives over IPC.
        return True

    def start(
        self,
        combo: Combo,
        on_trigger: TriggerCallback,
        on_hold: TriggerCallback | None = None,
    ) -> None:
        # Nothing to register. Triggers arrive via the IPC "toggle" command, which the app
        # wires straight to the same handler.
        #
        # `on_hold` is accepted and ignored: a desktop-bound command only reports that the
        # shortcut fired, never for how long, so there is nothing here that could tell a
        # tap from a hold. `yada retry` is the equivalent, bound to a second shortcut.
        self._combo = combo

    def stop(self) -> None:
        self._combo = None

    def problem(self) -> str | None:
        # Nothing to fail: the trigger arrives over IPC, so there is no grab to lose.
        return None

    def status(self) -> str:
        combo = self._combo.display if self._combo else "your shortcut"
        return (
            f"Bind {combo} in System Settings → Shortcuts to:\n"
            f"    {toggle_command()}\n"
            "yada cannot register this itself on Wayland, but the command reaches the "
            "running app instantly."
        )
