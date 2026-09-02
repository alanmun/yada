"""Redirecting into a newer installed version, without a separate launcher binary.

Why this module exists instead of the launcher shim it replaces:

yada used to ship a small one-file PyInstaller executable as a stable entry point at the
install root. Windows Defender classified it as Trojan:Win32/Bearfoos.A!ml -- a
machine-learning heuristic, not a signature -- and removed it along with the Start Menu
shortcut and the autostart key, roughly ninety seconds after install. The application
itself, built one-*dir*, was never touched.

That is not a surprising verdict in hindsight. A one-file build extracts itself to a temp
directory and runs code from there, which is the behaviour of a dropper, and this one then
registered itself for autostart from a freshly written unsigned binary. Signing would fix
the reputation problem properly, but not shipping a self-extracting executable at all is
both free and more honest.

So there is no launcher any more. Shortcuts point straight at a version's own executable,
and the running version is responsible for two things: handing over to a newer version if
one has been staged, and keeping the shortcuts pointing at whatever is current.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .updater.core import (
    InstalledVersion,
    mark_healthy,
    note_launch_attempt,
    read_current,
    select_version_to_launch,
    write_current,
)


def running_version_dir() -> Path | None:
    """The version directory this process is running out of, if it is a managed install."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def newer_staged_version() -> InstalledVersion | None:
    """A complete, non-broken version that is not the one currently executing.

    Returns None when we are already the best choice, which is the common case.
    """
    target = select_version_to_launch()
    if target is None:
        return None
    here = running_version_dir()
    if here is None:
        return None
    try:
        if target.path.resolve() == here:
            return None
    except OSError:
        return None
    return target


def hand_over(target: InstalledVersion, args: list[str]) -> bool:
    """Start `target` and report whether it was launched, so the caller can exit.

    Detached deliberately: this process is about to end and must not keep the new one
    tethered to a console or a dying process group.
    """
    executable = target.executable
    if not executable.exists():
        return False

    if read_current() != target.version:
        write_current(target.version)
    note_launch_attempt(target.version)

    command = [str(executable), *args]
    try:
        if sys.platform == "win32":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(command, close_fds=True, creationflags=0x00000008 | 0x00000200)
        else:
            subprocess.Popen(
                command,
                close_fds=True,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def redirect_if_newer(args: list[str]) -> bool:
    """Hand over to a newer staged version if there is one. True means "we are done".

    Called before the GUI starts, so an update applies by simply launching yada -- there
    is no install step and nothing for the user to click.
    """
    if os.environ.get("YADA_NO_REDIRECT"):
        return False
    target = newer_staged_version()
    if target is None:
        return False
    return hand_over(target, args)


def claim_healthy() -> None:
    """Record that the running version starts successfully.

    Paired with note_launch_attempt: three attempts without this and the version is
    presumed broken and skipped, which is what makes a bad release recoverable.
    """
    here = running_version_dir()
    if here is None:
        return
    version = here.name
    if version:
        mark_healthy(version)
