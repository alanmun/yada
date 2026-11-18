"""Test-wide setup.

Qt's platform plugin is pinned here so a local run and CI agree. They did not: CI sets
QT_QPA_PLATFORM=offscreen and nothing set it locally, so the same tests ran against
WSLg's real compositor. Focus behaves differently between the two, and a wheel-guard test
that had its premise quietly undermined passed on one and failed on the other -- for three
releases, in the code that exists to stop scrolling from corrupting settings.

Set QT_QPA_PLATFORM explicitly to test against a real display on purpose.
"""

from __future__ import annotations

import os

# Before any test module imports PySide6, which reads this at QApplication construction.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Likewise the desktop session. CI has none; a local run on Plasma Wayland would otherwise
# build the real RemoteDesktop portal backend, and any test that turns pasting on would put
# a genuine permission dialog on the developer's screen. Tests that care set these.
# The end-to-end session (scripts/e2e_kde.py) is the exception: it is private and headless,
# and its tests exist precisely to talk to that desktop.
if os.environ.get("YADA_E2E") != "1":
    for _name in ("XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "WAYLAND_DISPLAY"):
        os.environ.pop(_name, None)
