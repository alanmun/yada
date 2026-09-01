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
        # Wait briefly so the settings UI can report a conflict immediately rather than
        # claiming success and failing silently.
        self._ready.wait(timeout=3.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id is not None:
            # Wake the blocking GetMessage so the thread can exit and unregister.
            ctypes.windll.user32.PostThreadMessageW(
                wintypes.DWORD(self._thread_id), WM_QUIT, 0, 0
            )
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None

    def status(self) -> str:
        if self._error:
            return f"Shortcut not registered: {self._error}"
        if self._thread is not None and self._thread.is_alive():
            combo = self._combo.display if self._combo else "shortcut"
            return f"{combo} is registered globally."
        return "Shortcut is not running."

    # -- hotkey thread ------------------------------------------------------------------

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        assert self._combo is not None
        modifiers, vk = self._combo.to_win32()

        if not user32.RegisterHotKey(None, HOTKEY_ID, modifiers, vk):
            code = ctypes.get_last_error()
            self._error = (
                f"{self._combo.display} is already taken by another application"
                if code == 1409  # ERROR_HOTKEY_ALREADY_REGISTERED
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
