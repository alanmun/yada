"""Stable launcher.

Shortcuts, the Start Menu entry and the .desktop file all point at this and never change.
Its only job is to pick a release directory and hand off, which is what lets a
background-downloaded update activate itself without an installer.

Kept deliberately tiny: it is the one component that cannot be fixed by an update, so it
must contain as little logic as possible.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Imported from the leaf module on purpose: `core` is pure standard library, whereas the
# package's other modules need httpx and cryptography, which this bundle excludes.
from .updater.core import (
    note_launch_attempt,
    prune_old_versions,
    read_current,
    select_version_to_launch,
    versions_dir,
    write_current,
)

# Subcommands that talk to an already-running instance (or just print) rather than
# starting the GUI. Running one of these says nothing about whether this version boots, so
# it must not count towards the health accounting that drives automatic rollback --
# otherwise three hotkey presses on a never-yet-started install would look like three
# failed launches and demote a perfectly good version.
CLIENT_COMMANDS = frozenset(
    {
        "toggle",
        "settings",
        "stop",
        "status",
        "doctor",
        "version",
        "--version",
        "-V",
        "help",
        "--help",
        "-h",
    }
)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    target = select_version_to_launch()

    if target is None:
        # No managed install: either a dev checkout, or a broken installation. In a dev
        # checkout the app is importable, so run it in-process. In a packaged launcher Qt is
        # deliberately excluded from the bundle, so this import fails -- and that failure is
        # itself the diagnosis worth printing.
        try:
            from .app import main as app_main
        except ImportError:
            print(
                "yada could not find an installed version to launch.\n"
                f"Expected one under: {versions_dir()}\n"
                "Reinstall yada, or run it from a source checkout with `python -m yada`.",
                file=sys.stderr,
            )
            return 1
        return app_main(args)

    # The pointer flip: this single write is the whole "apply the update" step. Done before
    # handing off so a crash in the new version still leaves the intent recorded, and the
    # health counter can decide to fall back next time.
    if read_current() != target.version:
        write_current(target.version)
    # Only a real GUI launch counts: the app calls mark_healthy() once it is up, and these
    # two must describe the same event or rollback fires on the wrong signal.
    if not args or args[0] not in CLIENT_COMMANDS:
        note_launch_attempt(target.version)
    prune_old_versions()

    cmd = [str(target.executable), *args]
    if sys.platform == "win32":
        # execv on Windows detaches oddly from a console parent, so spawn instead. But the
        # two cases need opposite behaviour:
        #
        #   Launching the GUI -- detach and return, so a shell prompt comes straight back.
        #   A client subcommand -- wait and propagate the exit code, because the caller is
        #   reading it. Returning 0 immediately made `yada status` always report success,
        #   which silently broke every script and check that relied on it.
        if args and args[0] in CLIENT_COMMANDS:
            return subprocess.run(cmd, check=False).returncode
        subprocess.Popen(cmd, close_fds=True)
        return 0
    os.execv(str(target.executable), cmd)
    return 0  # unreachable on POSIX


if __name__ == "__main__":
    raise SystemExit(main())
