# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the stable launcher shim.

This is the binary shortcuts point at, and the one component an update cannot replace while
it is running -- so it is kept as small and as dumb as possible. It imports nothing but the
standard library and `yada.updater.core`: read a pointer file, pick a version directory, exec.

Everything Qt, audio and network related is excluded. If this needs a bug fix, that is a
reinstall, so the goal is for it never to need one.
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

a = Analysis(
    [str(ROOT / "packaging" / "entry_launcher.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[],
    hiddenimports=["yada", "yada.launcher", "yada.updater.core"],
    excludes=[
        "PySide6",
        "shiboken6",
        "numpy",
        "soxr",
        "sounddevice",
        "httpx",
        "websockets",
        "keyring",
        "cryptography",
        "dbus_fast",
        "tkinter",
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
    # Distinct build name so it does not collide with the app spec's dist/yada/
    # directory. CI renames it to "yada" when assembling the installer, since that
    # is the name shortcuts point at.
    name="yada-launcher",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)
