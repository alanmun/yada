"""Command line entry point.

Subcommands are deliberately few. `toggle` is the important one: it is what a desktop
shortcut binds to, so it runs on every keypress and must stay fast. It imports nothing
beyond the standard library before deciding whether an instance is already running -- pulling
in Qt first would add a visible delay to the one action that has to feel instant.
"""

from __future__ import annotations

import contextlib
import sys


def _stdout_usable() -> bool:
    """Whether we already have somewhere to write."""
    if sys.stdout is None:
        return False
    try:
        sys.stdout.write("")
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        return False
    return True


def _prepare_console() -> None:
    """On Windows, make CLI output both visible and encodable.

    Two independent problems:

    * yada.exe is built for the GUI subsystem, because a tray app must not flash a console
      window on every launch. The cost is that it starts with no stdout at all: run
      `yada doctor` from PowerShell and it prints nothing, succeeds, and tells you nothing.
      AttachConsole(ATTACH_PARENT_PROCESS) borrows the terminal that launched us.

    * The console's default code page is cp1252, which cannot encode the arrows and
      em-dashes in our own help text. Writing one raised UnicodeEncodeError and killed
      `yada doctor` partway through its report.

    These are deliberately handled in that order and *independently*. An earlier version
    returned early once it found a usable stdout, which meant a redirected stream -- the
    common case, including `yada doctor > out.txt` and every CI capture -- never got the
    encoding fix and kept crashing. A pipe is exactly as cp1252 as a console.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except ImportError:
        return

    if not _stdout_usable():
        try:
            ATTACH_PARENT_PROCESS = -1
            if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
                # Not context-managed on purpose: these replace the process-wide standard
                # streams and must stay open for the lifetime of the process.
                sys.stdout = open(  # noqa: SIM115
                    "CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace"
                )
                sys.stderr = open(  # noqa: SIM115
                    "CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace"
                )
                sys.stdin = open("CONIN$", encoding="utf-8", errors="replace")  # noqa: SIM115
        except Exception:  # noqa: BLE001 - no console is a normal state, never a failure
            pass

    # Unconditional: applies to a borrowed console and a redirected pipe alike.
    _force_utf8_streams(ctypes)


def _force_utf8_streams(ctypes_module) -> None:
    """Best-effort: UTF-8 console code page, and streams that never raise on encode."""
    with contextlib.suppress(Exception):  # no console, or a restricted one
        ctypes_module.windll.kernel32.SetConsoleOutputCP(65001)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # A pipe that refuses reconfiguration is not worth failing over.
        with contextlib.suppress(Exception):
            reconfigure(encoding="utf-8", errors="replace")

USAGE = """yada — Yet Another Dictating App

Usage:
  yada                 Start yada in the system tray (or focus a running instance)
  yada toggle          Start or stop recording in the running instance
  yada settings        Open the settings window
  yada stop            Quit the running instance
  yada status          Report whether yada is running
  yada doctor          Check whether this machine can run yada, and what is missing
  yada install         Install into your user profile and start (also happens on
                       first run from an extracted archive)
  yada --minimized     Start without opening the window (used by the login entry)
  yada --version       Print the version
  yada --help          Show this message

Bind `yada toggle` to a shortcut in your desktop's keyboard settings on Wayland, where
applications are not permitted to grab keys themselves.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    # Any subcommand may print. Bare `yada` starts the tray app and deliberately does not
    # borrow the terminal, so launching it from a shell returns the prompt immediately.
    if args:
        _prepare_console()

    minimized = "--minimized" in args
    if minimized:
        args = [a for a in args if a != "--minimized"]

    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if args and args[0] in ("-V", "--version", "version"):
        from . import __version__
        from .updater import read_current

        print(f"yada {read_current() or __version__}")
        return 0

    # Stdlib-only import: keep the hotkey path free of Qt.
    from . import ipc

    command = args[0] if args else None

    if command in ("toggle", "settings", "stop"):
        wire = {"stop": "quit"}.get(command, command)
        reply = ipc.send_command(wire)
        if reply is not None:
            return 0 if reply.get("ok") else 1
        if command == "stop":
            print("yada is not running.")
            return 1
        # Nothing listening: starting up and, for `toggle`, beginning to record is more
        # useful than reporting an error to a keypress.
        print("yada is not running — starting it.")
        return _run_app(start_recording=(command == "toggle"))

    if command == "--probe-tray":
        # Internal, used by `yada doctor`. Answering "is there a system tray" requires a
        # live QApplication, and constructing one can block inside a Win32 call while
        # holding the GIL -- which freezes the whole interpreter, so no in-process timeout
        # can rescue it. Run here in a child process the parent can kill instead.
        try:
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon

            app = QApplication([])
            print("1" if QSystemTrayIcon.isSystemTrayAvailable() else "0")
            app.quit()
        except Exception as exc:  # noqa: BLE001 - the parent only needs a verdict
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0

    if command in ("install", "--install"):
        return _install()

    if command == "doctor":
        # Deliberately ahead of the IPC checks: doctor must work whether or not yada is
        # running, and it is the first thing to reach for when it will not start.
        from .doctor import main as doctor_main

        return doctor_main()

    if command == "status":
        running = ipc.is_running()
        print("yada is running." if running else "yada is not running.")
        return 0 if running else 1

    if command is not None and command.startswith("-"):
        print(USAGE)
        return 2
    if command is not None:
        print(f"Unknown command {command!r}.\n")
        print(USAGE)
        return 2

    # Order matters here. Running from an extracted archive means yada is not installed
    # yet, and installing is the intent of the double-click -- even, especially, when an
    # older copy is already running. Checking "is one running" first meant 0.1.8's
    # executable found the running 0.1.7, forwarded a settings command to it, and exited:
    # the new version appeared to do nothing at all. install_and_relaunch stops whatever
    # is running before it replaces anything.
    from .selfinstall import is_managed_install

    if getattr(sys, "frozen", False) and not is_managed_install():
        return _install()

    # Bare `yada` from an installed copy: focus the existing instance rather than starting
    # a second one.
    if ipc.is_running():
        ipc.send_command("settings")
        return 0
    return _run_app(minimized=minimized)


def _install() -> int:
    """Install this payload and start the installed copy."""
    from .selfinstall import install_and_relaunch

    ok, message = install_and_relaunch()
    print(message)
    # Only failures get a dialog. On success the installed copy opens its own window in
    # the foreground, which is the confirmation -- a modal "it worked, click OK" in front
    # of it is pure friction.
    if not ok:
        _notify(message, error=True)
    return 0 if ok else 1


def _notify(message: str, *, error: bool = False) -> None:
    """Show a message box on Windows, where there may be no console to print to.

    yada.exe is built for the GUI subsystem, so a double-click from Explorer has nowhere
    to write. Without this, installing by double-clicking would appear to do nothing.

    Suppressed whenever nobody can click it. MessageBoxW blocks until dismissed, so an
    unattended run hangs until something kills it -- which is exactly what happened in CI,
    where `yada install` sat at a dialog until the step timed out. A console-attached run
    has already printed the same text, so the dialog would only be redundant there too.
    """
    import os

    if sys.platform != "win32":
        return
    if os.environ.get("CI") or os.environ.get("YADA_NO_DIALOG"):
        return
    if sys.stdout is not None and sys.stdout.isatty():
        return
    try:
        import ctypes

        icon = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, message, "yada", icon)
    except Exception:  # noqa: BLE001 - a missing dialog must not mask the outcome
        pass


def _run_app(
    *, start_recording: bool = False, minimized: bool = False, args: list[str] | None = None
) -> int:
    # Applying an update is just launching the newer version, so this is checked before
    # anything expensive is imported. Replaces the separate launcher binary that Windows
    # Defender quarantined; see relaunch.py.
    from .relaunch import redirect_if_newer

    if redirect_if_newer(args or []):
        return 0

    from .app import main as app_main

    return app_main(start_recording=start_recording, minimized=minimized)


if __name__ == "__main__":
    raise SystemExit(main())
