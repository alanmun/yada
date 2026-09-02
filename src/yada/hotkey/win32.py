"""Windows global hotkey via RegisterHotKey.

RegisterHotKey has one firm rule: the hotkey must be registered on the same thread that
runs the message loop which receives WM_HOTKEY. So this backend owns a dedicated thread that
registers, pumps messages, and unregisters. No admin rights, no low-level keyboard hook, no
antivirus false positives -- which is why this is preferred over the `keyboard`/`pynput`
approach of installing a system-wide hook.

MOD_NOREPEAT is set, so holding the combo fires once rather than starting and stopping
recording dozens of times.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from .base import Combo, TriggerCallback

HOTKEY_ID = 1
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# A combo held by a process that is on its way out cannot be registered until it goes, and
# that is the ordinary case: yada replacing yada. Retrying briefly turns "the shortcut is
# taken" -- reported once, in a tab the user has no reason to open -- into a shortcut that
# simply works a second later.
REGISTER_ATTEMPTS = 6
REGISTER_RETRY_DELAY = 0.5


def _user32():
    """user32 with `use_last_error`, which is what makes GetLastError readable.

    `ctypes.windll.user32` does not set it, so `ctypes.get_last_error()` against that
    handle returns 0. A failed registration therefore reported "RegisterHotKey failed
    (error 0)" -- which is why an instance that never registered the shortcut looked
    identical to one that had.
    """
    return ctypes.WinDLL("user32", use_last_error=True)


class Win32HotkeyBackend:
    name = "win32"

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: str | None = None
        self._combo: Combo | None = None
        self._on_trigger: TriggerCallback | None = None

    @staticmethod
    def available() -> bool:
        import sys

        return sys.platform == "win32"

    def start(self, combo: Combo, on_trigger: TriggerCallback) -> None:
        self._combo = combo
        self._on_trigger = on_trigger
        self._error = None
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="yada-hotkey", daemon=True)
        self._thread.start()
        # Must outlast the registration retries, or the settings pane reports "not
        # running" for a shortcut that is about to come up.
        self._ready.wait(timeout=REGISTER_ATTEMPTS * REGISTER_RETRY_DELAY + 2.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id is not None:
            # Wake the blocking GetMessage so the thread can exit and unregister.
            ctypes.windll.user32.PostThreadMessageW(wintypes.DWORD(self._thread_id), WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None

    def problem(self) -> str | None:
        return self._error

    def status(self) -> str:
        if self._error:
            return f"Shortcut not registered: {self._error}"
        if self._thread is not None and self._thread.is_alive():
            combo = self._combo.display if self._combo else "shortcut"
            return f"{combo} is registered globally."
        return "Shortcut is not running."

    # -- hotkey thread ------------------------------------------------------------------

    def _run(self) -> None:
        import time

        user32 = _user32()
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        assert self._combo is not None
        modifiers, vk = self._combo.to_win32()

        code = 0
        for attempt in range(REGISTER_ATTEMPTS):
            ctypes.set_last_error(0)
            if user32.RegisterHotKey(None, HOTKEY_ID, modifiers, vk):
                code = 0
                break
            code = ctypes.get_last_error()
            # Only a conflict is worth waiting out; anything else will not fix itself.
            if code != ERROR_HOTKEY_ALREADY_REGISTERED:
                break
            if attempt < REGISTER_ATTEMPTS - 1 and not self._stop.is_set():
                time.sleep(REGISTER_RETRY_DELAY)
        if code:
            self._error = (
                f"{self._combo.display} is already taken by another application"
                if code == ERROR_HOTKEY_ALREADY_REGISTERED
                else f"RegisterHotKey failed (error {code})"
            )
            self._ready.set()
            return

        self._ready.set()
        msg = wintypes.MSG()
        try:
            while not self._stop.is_set():
                # GetMessage blocks until a message arrives; PostThreadMessage(WM_QUIT)
                # is what unblocks it on shutdown.
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):  # WM_QUIT, or an error
                    break
                if msg.message == WM_HOTKEY and self._on_trigger is not None:
                    self._on_trigger()
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
