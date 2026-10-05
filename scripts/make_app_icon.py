#!/usr/bin/env python
"""Export the existing microphone artwork for Windows executables and Linux launchers.

Run with QT_QPA_PLATFORM=offscreen uv run python scripts/make_app_icon.py.
PNG-backed ICO frames keep transparency at every Windows display size without Pillow.
"""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWidgets import QApplication

from yada.ui.icons import APP_SIZES, app_icon


def main() -> None:
    app = QApplication.instance() or QApplication([])
    icon = app_icon()
    frames = []
    for size in APP_SIZES:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not icon.pixmap(size, size).save(buffer, "PNG"):
            raise RuntimeError(f"Could not render {size}px icon")
        frames.append(bytes(buffer.data()))
    offset = 6 + 16 * len(frames)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    for size, frame in zip(APP_SIZES, frames, strict=True):
        directory.extend(
            struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(frame), offset)
        )
        offset += len(frame)
    assets = Path(__file__).resolve().parents[1] / "src/yada/assets/icons"
    (assets / "yada.ico").write_bytes(directory + b"".join(frames))
    (assets / "yada.png").write_bytes(frames[-1])
    print(f"Wrote {len(frames)} ICO sizes and a 256px PNG to {assets}")
    app.quit()


if __name__ == "__main__":
    main()
