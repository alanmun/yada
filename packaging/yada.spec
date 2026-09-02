# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the yada application binary.

One-dir rather than one-file. One-file re-extracts the whole bundle to a temp directory on
every launch, which adds seconds to startup -- unacceptable for something that lives in the
tray and is expected to respond to a keypress. One-dir also means the updater can swap a
directory atomically, which is exactly the shape the versioned-install layout wants.

Produces:  dist/yada/yada[.exe]  plus  dist/yada/_internal/
which is archived as the release asset and extracted into versions/<version>/.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

# Every directory under src/yada/assets, because forgetting one ships a build that runs and
# is quietly wrong. `icons` was missing from this list for ten releases: the checkbox tick
# and the spin-box arrows are `image: url(...)` in the stylesheet, and Qt draws nothing at
# all for a missing file -- so the tick simply did not exist in any released build, while
# working perfectly from a source checkout. Enumerating the directory means the next asset
# folder is included by existing.
ASSET_ROOT = SRC / "yada" / "assets"
_asset_dirs = sorted(p for p in ASSET_ROOT.iterdir() if p.is_dir())
if not _asset_dirs:
    raise SystemExit(f"no asset directories found under {ASSET_ROOT}")
for _required in ("sounds", "icons"):
    # Named explicitly as well: an empty or renamed directory would otherwise pass silently
    # and produce a build that is silent, or one with invisible checkboxes.
    if not (ASSET_ROOT / _required).is_dir():
        raise SystemExit(f"required assets are missing: {ASSET_ROOT / _required}")
    if not any((ASSET_ROOT / _required).iterdir()):
        raise SystemExit(f"required assets directory is empty: {ASSET_ROOT / _required}")

datas = [(str(d), f"yada/assets/{d.name}") for d in _asset_dirs]

binaries = []


def _find_portaudio():
    """Locate libportaudio for bundling.

    Necessary because `sounddevice` is a single module that dlopen()s PortAudio at import
    time -- PyInstaller cannot see that dependency by static analysis. On Windows and macOS
    the wheel ships `_sounddevice_data`, which the contrib hook collects; on Linux the wheel
    does not, so the system library has to be bundled explicitly.

    Getting this wrong produces a build that starts fine and then cannot record, which is a
    much worse failure than not building at all.
    """
    import ctypes.util
    import subprocess

    soname = ctypes.util.find_library("portaudio")
    if not soname:
        return []
    if Path(soname).is_absolute():
        return [(soname, ".")]
    try:
        out = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return []
    for line in out.splitlines():
        if soname in line and "=>" in line:
            return [(line.split("=>")[-1].strip(), ".")]
    return []


# Bundled by the wheel on Windows/macOS; collected by the contrib hook when present.
try:
    binaries += collect_dynamic_libs("_sounddevice_data")
    datas += collect_data_files("_sounddevice_data")
except Exception:  # noqa: BLE001 - absent on Linux, which the next line covers
    pass
if sys.platform.startswith("linux"):
    portaudio = _find_portaudio()
    if not portaudio:
        raise SystemExit(
            "libportaudio was not found. Install it before building "
            "(apt install libportaudio2), or the packaged app will start but not record."
        )
    binaries += portaudio

# soxr bundles its own native resampler.
binaries += collect_dynamic_libs("soxr")

hiddenimports = [
    "yada.providers.openai_provider",
    "yada.providers.openrouter",
    # Selected at runtime by platform, so static analysis does not see the import.
    "yada.hotkey.win32",
    "yada.hotkey.kde_portal",
    "yada.hotkey.external",
]

# Qt is the bulk of the download. Excluding what yada cannot use roughly halves it.
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtSerialPort",
    "tkinter",
    "matplotlib",
    "PIL",
    "pytest",
]
# QtMultimedia is required -- QSoundEffect plays the chimes. Never add it above.

a = Analysis(
    [str(ROOT / "packaging" / "entry_app.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yada",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed binaries trip antivirus heuristics for no real size win here
    console=False,  # a tray app must not flash a console window on Windows
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="yada",
)
