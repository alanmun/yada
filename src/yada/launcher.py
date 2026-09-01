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

from .updater.core import (
    note_launch_attempt,
    prune_old_versions,
    read_current,
    select_version_to_launch,
    write_current,
)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    target = select_version_to_launch()

    if target is None:
        # No managed install (a dev checkout, or a first run before any release is
        # staged). Run the app in-process rather than failing.
        from .app import main as app_main

        return app_main(args)

    # The pointer flip: this single write is the whole "apply the update" step. Done before
    # handing off so a crash in the new version still leaves the intent recorded, and the
    # health counter can decide to fall back next time.
    if read_current() != target.version:
        write_current(target.version)
    note_launch_attempt(target.version)
    prune_old_versions()

    cmd = [str(target.executable), *args]
    if sys.platform == "win32":
        # execv on Windows detaches oddly from a console parent; spawn and exit instead.
        subprocess.Popen(cmd, close_fds=True)
        return 0
    os.execv(str(target.executable), cmd)
    return 0  # unreachable on POSIX


if __name__ == "__main__":
    raise SystemExit(main())
