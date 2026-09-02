# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the double-click installer.

Console mode on purpose: it prints what it is doing and pauses on exit, so a
double-click from Explorer shows progress and, more importantly, shows errors instead of
a window that vanishes.

Stdlib only — no Qt, no numpy, no network stack. It must be able to install an app that
would not itself start.
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "installer.py")],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=[
        "PySide6", "shiboken6", "numpy", "soxr", "sounddevice", "httpx", "websockets",
        "keyring", "cryptography", "dbus_fast", "tkinter", "yada",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="INSTALL",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=None,
)
