"""Finding out whether another process is really still there, and ending it if it won't go.

The installer needs this. Waiting on the IPC socket is not good enough, and the difference
is not academic: `CommandServer.stop()` runs early in shutdown, so the socket disappears
while the process is still alive with its DLLs mapped. An install that treated the closed
socket as "it's gone" started deleting a version directory out from under a running copy,
got most of the way through, and then hit `python3.dll` -- which Windows will not let you
delete while it is mapped. The user was left with a half-deleted install and an app that
would not start.

So processes are found by their executable path rather than by asking them nicely. That
also means it works against copies of yada that predate this module: there is no
cooperation required from the process being replaced.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

# After a polite request is ignored, how long a terminated process gets to disappear.
TERMINATE_GRACE = 5.0


def _resolve(path: Path | str) -> Path:
    with contextlib.suppress(OSError):
        return Path(path).resolve()
    return Path(path)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102


def _win32():
    """Bind the handful of calls we need. Kept lazy so importing on Linux is free."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    # restype on OpenProcess matters: the default int truncates a 64-bit handle, which
    # fails in ways that look like the process having vanished.
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    psapi.EnumProcesses.restype = wintypes.BOOL
    psapi.EnumProcesses.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    return ctypes, wintypes, kernel32, psapi


def _win_pids() -> list[int]:
    ctypes, wintypes, _kernel32, psapi = _win32()
    count = 1024
    while count <= 1 << 20:
        buf = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return []
        if needed.value < ctypes.sizeof(buf):
            return list(buf[: needed.value // ctypes.sizeof(wintypes.DWORD)])
        count *= 2  # the buffer filled exactly; assume it was truncated
    return []


def _win_image(pid: int) -> str | None:
    ctypes, wintypes, kernel32, _psapi = _win32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _win_alive(pid: int) -> bool:
    _ctypes, _wintypes, kernel32, _psapi = _win32()
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if handle:
        try:
            return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    # SYNCHRONIZE was refused. Query-limited access is granted more widely, so use it to
    # tell "not permitted" apart from "not there" -- guessing "gone" would hand the
    # installer a green light to start deleting.
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def _win_terminate(pid: int) -> None:
    _ctypes, _wintypes, kernel32, _psapi = _win32()
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------------------
# POSIX
# --------------------------------------------------------------------------------------


def _posix_pids() -> list[int]:
    try:
        return [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return []


def _posix_image(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def _posix_zombie(pid: int) -> bool:
    """True for a process that has exited but has not been reaped by its parent.

    Signal 0 succeeds against a zombie, so without this check an exited process reads as
    alive for as long as its parent ignores it -- and the installer would wait out its
    whole timeout before declaring it unkillable. A zombie holds no handles and no
    mappings, which is the only thing being asked here.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # The comm field is parenthesised and may itself contain spaces and brackets, so the
    # state character is the first field after the final ')'.
    _, _, remainder = stat.rpartition(")")
    fields = remainder.split()
    return bool(fields) and fields[0] == "Z"


def _posix_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, but it exists
    except OSError:
        return False
    return not _posix_zombie(pid)


def _posix_terminate(pid: int) -> None:
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


# --------------------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_alive(pid)
    return _posix_alive(pid)


def process_image(pid: int) -> str | None:
    """Full path to a process's executable, or None if it cannot be determined."""
    if sys.platform == "win32":
        return _win_image(pid)
    return _posix_image(pid)


def processes_under(directory: Path) -> list[int]:
    """PIDs of processes whose executable lives inside `directory`.

    This is the question the installer actually has: not "is something listening" but "is
    anything running out of the files I am about to replace". Our own PID is excluded --
    the installer is itself a yada executable, and killing it would be a short career.
    """
    root = _resolve(directory)
    if not root.exists():
        return []
    me = os.getpid()
    pids = _win_pids() if sys.platform == "win32" else _posix_pids()
    found = []
    for pid in pids:
        if pid == me or pid <= 0:
            continue
        image = process_image(pid)
        if image and _is_within(_resolve(image), root):
            found.append(pid)
    return found


def wait_for_exit(pids: list[int], timeout: float) -> list[int]:
    """Wait for every PID to disappear. Returns whichever are still alive."""
    deadline = time.monotonic() + timeout
    remaining = [p for p in pids if pid_alive(p)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.2)
        remaining = [p for p in remaining if pid_alive(p)]
    return remaining


def terminate(pids: list[int], *, grace: float = TERMINATE_GRACE) -> list[int]:
    """End these processes. Returns whichever survived anyway."""
    for pid in pids:
        if sys.platform == "win32":
            _win_terminate(pid)
        else:
            _posix_terminate(pid)
    survivors = wait_for_exit(pids, grace)
    if survivors and sys.platform != "win32":
        for pid in survivors:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        survivors = wait_for_exit(survivors, grace)
    return survivors
