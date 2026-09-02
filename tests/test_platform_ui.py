"""Platform-specific advice must not leak across platforms.

Reading about Wayland key-grab restrictions on Windows 11 is worse than unhelpful -- it
implies a limitation that does not exist there, and points at settings the user does not
have.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

from yada.config import Settings
from yada.ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _all_text(widget) -> str:
    parts = [lbl.text() for lbl in widget.findChildren(QLabel)]
    from PySide6.QtWidgets import QAbstractButton, QComboBox

    parts += [b.text() for b in widget.findChildren(QAbstractButton)]
    for combo in widget.findChildren(QComboBox):
        parts += [combo.itemText(i) for i in range(combo.count())]
    return "\n".join(parts)


def _window(qapp, tmp_path, monkeypatch, platform: str, backends: list[str]):
    """Build the settings window as it would appear on `platform`.

    The paste description comes from whichever backend is available, which is decided in
    output.paste rather than here -- so that is substituted too, otherwise the simulation
    reports the host's paste situation instead of the platform under test.
    """
    import yada.config as cfg
    import yada.output.sounds as snd
    import yada.ui.settings_window as sw
    from yada.output.paste import NoPasteBackend, Win32PasteBackend, YdotoolPasteBackend

    monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(snd, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sw, "sys", type("sys", (), {"platform": platform}))
    monkeypatch.setattr(sw, "available_backends", lambda: backends)

    backend = Win32PasteBackend() if platform == "win32" else YdotoolPasteBackend()
    assert not isinstance(backend, NoPasteBackend)
    monkeypatch.setattr(sw, "create_paste_backend", lambda: backend)
    return SettingsWindow(Settings())


def test_windows_shows_no_wayland_advice(qapp, tmp_path, monkeypatch):
    text = _all_text(_window(qapp, tmp_path, monkeypatch, "win32", ["win32", "external"]))
    for term in ("Wayland", "ydotool", "System Settings", "kde", "KDE"):
        assert term not in text, f"{term!r} must not appear on Windows"
    assert "Registered with Windows directly" in text


def test_windows_does_not_offer_the_wayland_portal_backend(qapp, tmp_path, monkeypatch):
    window = _window(qapp, tmp_path, monkeypatch, "win32", ["win32", "external"])
    offered = [
        window.hotkey_backend.itemData(i) for i in range(window.hotkey_backend.count())
    ]
    assert "kde_portal" not in offered
    assert offered == ["auto", "win32", "external"]


def test_linux_keeps_the_wayland_advice(qapp, tmp_path, monkeypatch):
    text = _all_text(
        _window(qapp, tmp_path, monkeypatch, "linux", ["kde_portal", "external"])
    )
    assert "Wayland" in text, "the restriction is real on Linux and must be explained"
    assert "Copy the command to bind" in text


def test_linux_does_not_offer_the_windows_backend(qapp, tmp_path, monkeypatch):
    window = _window(qapp, tmp_path, monkeypatch, "linux", ["kde_portal", "external"])
    offered = [
        window.hotkey_backend.itemData(i) for i in range(window.hotkey_backend.count())
    ]
    assert "win32" not in offered


def test_the_real_platform_offers_only_real_backends(qapp, tmp_path, monkeypatch):
    """Guards against the lists drifting apart from what hotkey.create_backend accepts."""
    from yada.hotkey import available_backends

    offered = set(available_backends())
    assert offered <= {"win32", "kde_portal", "external"}
    assert "external" in offered, "there must always be a usable fallback"
    if sys.platform == "win32":
        assert "win32" in offered
    else:
        assert "win32" not in offered
