"""Command line entry point.

Subcommands are deliberately few. `toggle` is the important one: it is what a desktop
shortcut binds to, so it runs on every keypress and must stay fast. It imports nothing
beyond the standard library before deciding whether an instance is already running -- pulling
in Qt first would add a visible delay to the one action that has to feel instant.
"""

from __future__ import annotations

import sys


def _attach_parent_console() -> None:
    """On Windows, reattach to the calling terminal so CLI output is actually visible.

    yada.exe is built for the GUI subsystem, because a tray app must not flash a console
    window on every launch. The cost is that it starts with no stdout at all: run
    `yada doctor` from PowerShell and it prints nothing, succeeds, and tells you nothing.

    AttachConsole(ATTACH_PARENT_PROCESS) borrows the terminal that launched us, then the
    standard streams are reopened onto it. Fails harmlessly when there is no parent console
    (launched from a shortcut, or from the hotkey), which is exactly when nobody is reading.
    """
    if sys.platform != "win32":
        return

    # If stdout already works, leave it alone. A GUI-subsystem process launched with its
    # output redirected (`yada doctor > out.txt`, or captured by a CI step) does get real
    # handles, and reopening CONOUT$ would write past the redirection to the console
    # device instead -- output would appear on screen but vanish from the capture.
    if sys.stdout is not None:
        try:
            sys.stdout.write("")
            sys.stdout.flush()
            return
        except (OSError, ValueError, AttributeError):
            pass

    try:
        import ctypes

        ATTACH_PARENT_PROCESS = -1
        if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return
        # Not context-managed on purpose: these replace the process-wide standard streams
        # and must stay open for the lifetime of the process.
        sys.stdout = open(  # noqa: SIM115
            "CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace"
        )
        sys.stderr = open(  # noqa: SIM115
            "CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace"
        )
        sys.stdin = open("CONIN$", encoding="utf-8", errors="replace")  # noqa: SIM115
    except Exception:  # noqa: BLE001 - no console is a normal state, never a failure
        return

USAGE = """yada — Yet Another Dictating App

Usage:
  yada                 Start yada in the system tray (or focus a running instance)
  yada toggle          Start or stop recording in the running instance
  yada settings        Open the settings window
  yada stop            Quit the running instance
  yada status          Report whether yada is running
  yada doctor          Check whether this machine can run yada, and what is missing
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
        _attach_parent_console()

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

    # Bare `yada`: focus an existing instance rather than starting a second one.
    if ipc.is_running():
        ipc.send_command("settings")
        return 0
    return _run_app()


def _run_app(*, start_recording: bool = False) -> int:
    from .app import main as app_main

    return app_main(start_recording=start_recording)


if __name__ == "__main__":
    raise SystemExit(main())
