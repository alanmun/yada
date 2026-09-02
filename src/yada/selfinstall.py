"""Installing yada, performed by yada itself.

There used to be a separate INSTALL.exe. It was a one-file PyInstaller build, and Windows
Defender classified it as Trojan:Win32/Bearfoos.A!ml and deleted it straight out of the
extracted archive -- the same verdict, for the same reason, as the launcher binary before
it. Worse, because that installer travels *inside* the update archive, the real-time scan
also interfered while the updater was extracting a release, which surfaced as the baffling
"extracted release 0.1.7 contains no yada.exe".

The application is a one-dir build and has never been flagged. So it does the installing:
the archive contains no self-extracting executable at all, and the file you double-click is
the app.

Running from an extracted archive is "portable mode": there is no managed install layout
around the executable. In that state yada installs itself into the per-user location and
relaunches from there.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import ipc
from .updater.core import (
    SwapFailed,
    executable_name,
    install_root,
    prune_old_versions,
    versions_dir,
    write_current,
)
from .updater.core import swap_in as core_swap_in

# How long to wait for an already-running copy to shut down before giving up on a clean
# handover. Generous: it has a transcription to finish and sockets to release.
SHUTDOWN_TIMEOUT = 20.0


class InstallError(RuntimeError):
    """Message is written to be shown to the user verbatim."""


def payload_dir() -> Path:
    """The directory the running executable lives in.

    The override exists so the install path can be exercised without a frozen build: from
    a source checkout `sys.executable` is the interpreter, which says nothing about where
    a payload is.
    """
    if override := os.environ.get("YADA_PAYLOAD_DIR"):
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def executable_dir() -> Path:
    """Where this process's executable actually lives.

    Deliberately ignores YADA_PAYLOAD_DIR. That override says where files to *install*
    come from, which is a different question from where we are running, and conflating the
    two produced an infinite install loop: the freshly installed copy inherited the
    variable, concluded it was a payload, and installed itself again.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def is_managed_install() -> bool:
    """True when running from `<install root>/versions/<v>/`.

    That is the shape the updater maintains. Anything else -- an extracted archive in
    Downloads, say -- means yada has not been installed yet.
    """
    return executable_dir().parent.name == "versions"


def payload_version() -> str:
    """The version being installed, from the VERSION file CI writes into every archive.

    Not from the binary: yada.exe is built for the Windows GUI subsystem and has no stdout,
    so asking it returns an empty string.
    """
    if env := os.environ.get("YADA_VERSION"):
        return env.strip()
    version_file = payload_dir() / "VERSION"
    if version_file.exists():
        text = version_file.read_text(encoding="utf-8").strip()
        if text:
            return text
    from . import __version__

    return __version__


def stop_running_instance(*, timeout: float = SHUTDOWN_TIMEOUT) -> bool:
    """Ask any running copy to quit, and make sure it has actually gone.

    Necessary rather than polite. On Windows a running instance holds its own executable
    and DLLs open, so replacing the version directory it occupies fails -- and installing
    while it runs leaves two copies fighting over one command socket and one microphone.

    This used to wait for the IPC socket to close, which is the wrong signal by a wide
    margin: the command server stops early in shutdown, so the socket goes quiet while the
    process is still running with its DLLs mapped. The wait now watches the processes
    themselves, and a copy that ignores the request is ended rather than left in place.
    Being told to go and close an app that is not responding is not an answer, and sparing
    the user that is the whole reason this function exists.
    """
    import time

    from . import procutil

    running = procutil.processes_under(install_root())
    if not running and not ipc.is_running():
        return True

    ipc.send_command("quit")  # a no-op if nothing is listening

    deadline = time.monotonic() + timeout
    while True:
        running = [pid for pid in running if procutil.pid_alive(pid)]
        if not running and not ipc.is_running():
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    if running and procutil.terminate(running):
        return False
    # Nothing of ours is running now. A socket still answering at this point belongs to a
    # copy somewhere else entirely -- another extracted archive, say -- and killing that
    # is not ours to do, so report the failure and let the message explain.
    return not ipc.is_running()


def _swap_in(incoming: Path, target: Path) -> None:
    """Install `incoming` over `target`, keeping the old version if anything goes wrong.

    The guarantee comes from `core.swap_in`: on failure the existing install is untouched,
    so the message can say so plainly. An earlier version of this deleted the target file
    by file and could leave it half gone, which is the one outcome a user cannot recover
    from without downloading again.
    """
    try:
        core_swap_in(incoming, target)
    except SwapFailed as exc:
        raise InstallError(
            f"Could not replace the existing {target.name} install ({exc}).\n"
            "Your working copy has been left exactly as it was."
        ) from exc


def install(*, version: str | None = None) -> Path:
    """Copy this payload into the managed location. Returns the installed executable."""
    here = payload_dir()
    version = version or payload_version()
    exe = executable_name()
    if not (here / exe).exists():
        raise InstallError(f"This folder does not contain {exe}. Extract the whole archive.")

    versions = versions_dir()
    versions.mkdir(parents=True, exist_ok=True)
    target = versions / version
    incoming = versions / f".incoming-{version}-{os.getpid()}"
    shutil.rmtree(incoming, ignore_errors=True)

    try:
        # The whole payload, verbatim. An earlier version picked out the executable,
        # _internal and a couple of known files, and the result segfaulted: a PyInstaller
        # one-dir bundle is not reliably reproducible by copying the parts you remember,
        # and on Linux it contains symlinks that must stay symlinks. Copying everything
        # cannot forget a file, and symlinks=True preserves the bundle exactly as the
        # archive was extracted.
        shutil.copytree(here, incoming, symlinks=True)

        exe_path = incoming / exe
        if not exe_path.exists():
            raise InstallError(f"The copied files do not contain {exe}.")
        if sys.platform != "win32":
            exe_path.chmod(0o755)

        _swap_in(incoming, target)
    except BaseException:
        shutil.rmtree(incoming, ignore_errors=True)
        raise

    # Written last: the only proof a version is usable, so an interrupted install is
    # ignored rather than half-booted.
    (target / ".complete").write_text(version + "\n", encoding="utf-8")
    write_current(version)

    # Stale leftovers only: another copy may have a download in flight, and wiping the
    # folder makes it fail with a bare errno.
    from .updater.github import clear_stale_downloads

    with contextlib.suppress(OSError):
        clear_stale_downloads()
    prune_old_versions()
    return target / exe


# Variables that describe *this* payload and must not reach the installed copy.
_PAYLOAD_ONLY_ENV = ("YADA_PAYLOAD_DIR", "YADA_VERSION")


def relaunch(executable: Path, args: list[str] | None = None) -> bool:
    """Start the installed copy, detached, and report whether it launched.

    The child gets a cleaned environment. Passing ours through meant the installed copy
    inherited YADA_PAYLOAD_DIR, decided it was an uninstalled payload, and installed
    itself again -- an install loop that never started the app.
    """
    command = [str(executable), *(args or [])]
    child_env = {k: v for k, v in os.environ.items() if k not in _PAYLOAD_ONLY_ENV}
    try:
        if sys.platform == "win32":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                command,
                close_fds=True,
                env=child_env,
                creationflags=0x00000008 | 0x00000200,
            )
        else:
            subprocess.Popen(
                command,
                close_fds=True,
                env=child_env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def install_and_relaunch() -> tuple[bool, str]:
    """The whole first-run path. Returns (success, message for the user)."""
    version = payload_version()
    if not stop_running_instance():
        # Reached only when a copy could not be ended: one running from outside the
        # install location, or a process this user cannot signal. A running copy of the
        # install itself is now closed rather than complained about.
        return False, (
            "Another copy of yada is running and could not be closed automatically.\n\n"
            "Quit it from its tray icon, then try again."
        )
    try:
        executable = install(version=version)
    except InstallError as exc:
        return False, str(exc)
    except OSError as exc:
        return False, f"Install failed: {exc}"

    if not relaunch(executable):
        return True, (
            f"yada {version} was installed to:\n{executable}\n\n"
            "It could not be started automatically; launch it from your Start Menu."
        )
    return True, (
        f"yada {version} is installed and running.\n\n"
        "On Windows 11 its tray icon starts out hidden behind the ^ arrow on the taskbar."
    )
