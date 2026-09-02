#!/usr/bin/env python
"""Generate the checkbox tick and spin-box arrows used by the blue theme.

Qt stylesheets cannot draw a checkmark -- `image:` needs a real file, and data URIs are not
supported. Leaving the indicator unstyled is not an option either: the native style drew a
near-black tick on the dark palette, which was invisible, and merely resizing the indicator
did not change its colour.

So the tick is a shipped asset, drawn white, used on the highlight-coloured box. The
spin-box arrows are here for the same reason, plus one of their own: sizing `::up-button`
explicitly is what makes the arrows scale with the text setting, and a styled button draws
no arrow of its own.

Regenerate with:  .venv/bin/python scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication

ASSETS = Path(__file__).resolve().parent.parent / "src" / "yada" / "assets" / "icons"

# Generated at several sizes because the indicator scales with the text setting, and a
# stretched bitmap tick looks soft.
SIZES = (16, 20, 24, 28, 32, 40)


def tick(size: int, colour: str) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Proportional to the box, with a stroke heavy enough to survive at 16px.
    path = QPainterPath()
    path.moveTo(QPointF(size * 0.22, size * 0.53))
    path.lineTo(QPointF(size * 0.42, size * 0.72))
    path.lineTo(QPointF(size * 0.79, size * 0.28))

    pen = QPen(QColor(colour))
    pen.setWidthF(max(1.8, size * 0.13))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPath(path)
    painter.end()
    return pm


def chevron(size: int, colour: str, *, up: bool) -> QPixmap:
    """A spin-box arrow. Drawn as a chevron so it stays legible at 8px.

    A filled triangle turns into a smudge at the sizes these buttons use; two strokes with
    a round join keep their shape.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    left, right = size * 0.26, size * 0.74
    near, far = (size * 0.62, size * 0.38) if up else (size * 0.38, size * 0.62)
    path = QPainterPath()
    path.moveTo(QPointF(left, near))
    path.lineTo(QPointF(size * 0.5, far))
    path.lineTo(QPointF(right, near))

    pen = QPen(QColor(colour))
    pen.setWidthF(max(1.6, size * 0.12))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPath(path)
    painter.end()
    return pm


def main() -> int:
    QApplication([])
    ASSETS.mkdir(parents=True, exist_ok=True)
    print("Writing tick icons:")
    for size in SIZES:
        path = ASSETS / f"check-{size}.png"
        tick(size, "#ffffff").save(str(path))
        print(f"  {path.name}")
    print("Writing spin-box arrows:")
    for size in SIZES:
        for name, up in (("up", True), ("down", False)):
            path = ASSETS / f"arrow-{name}-{size}.png"
            chevron(size, "#e8f0fb", up=up).save(str(path))
            print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
