"""KWin places the overlay on KDE Wayland, because Wayland ignores QWidget.move().

The script and the overlay agree on a window title; if either side drifts, the overlay
silently goes back to the middle of the screen. These tests pin that contract.
"""

from __future__ import annotations

import json

import pytest

from yada.ui import kwin_placement


def test_script_matches_the_overlay_title_and_class():
    source = kwin_placement.script_source()
    assert f"const TITLE = {json.dumps(kwin_placement.OVERLAY_TITLE)};" in source
    assert 'w.resourceClass === "yada"' in source  # setDesktopFileName("yada") in app.py


def test_script_avoids_the_qt_global_plain_kwin_scripts_lack():
    assert "Qt." not in kwin_placement.script_source()


@pytest.mark.parametrize(
    ("session", "desktop", "expected"),
    [
        ("wayland", "KDE", True),
        ("wayland", "ubuntu:KDE", True),
        ("x11", "KDE", False),
        ("wayland", "GNOME", False),
        ("wayland", "", False),
    ],
)
def test_only_kde_wayland_needs_it(monkeypatch, session, desktop, expected):
    monkeypatch.setenv("XDG_SESSION_TYPE", session)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    assert kwin_placement.kde_wayland_session() is expected
