"""Tray icons, drawn at runtime instead of shipped as PNGs.

Two reasons: they stay crisp at any tray size and DPI without shipping six variants, and
there are no image assets to keep in sync with the states enum.

Design constraint: a tray icon is often 16 px. Detail is wasted there, so state is carried by
*colour on a filled disc* -- unmistakable at a glance and across light and dark trays --
with a simple microphone silhouette to say which app it is.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

from ..pipeline.session import SessionState

# Rendered at these sizes so the tray always has an exact match rather than a scaled one.
SIZES = (16, 20, 24, 32, 48, 64)

# Slate when idle, red while recording, amber while working. Chosen to stay distinguishable
# for the common forms of colour blindness by differing in lightness as well as hue.
STATE_COLOURS: dict[SessionState, str] = {
    SessionState.IDLE: "#5b6472",
    SessionState.RECORDING: "#d9342b",
    SessionState.TRANSCRIBING: "#e08c1a",
    SessionState.TRANSFORMING: "#c065d0",
}


def _mic_path(size: float) -> QPainterPath:
    """A microphone silhouette in a `size` x `size` box, kept deliberately chunky.

    Capsule, stand, foot. An earlier version drew the cradle arc too, but at 16-24 px it was
    invisible, so it was removed rather than left as decoration that costs code.
    """
    path = QPainterPath()
    body_w = size * 0.30
    body_h = size * 0.44
    path.addRoundedRect(
        QRectF((size - body_w) / 2, size * 0.18, body_w, body_h), body_w / 2, body_w / 2
    )
    stand_w = size * 0.10
    path.addRect(QRectF((size - stand_w) / 2, size * 0.64, stand_w, size * 0.12))
    foot_w = size * 0.34
    path.addRoundedRect(
        QRectF((size - foot_w) / 2, size * 0.74, foot_w, size * 0.08),
        size * 0.04,
        size * 0.04,
    )
    return path.simplified()


def _pixmap(state: SessionState, size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Filled disc carrying the state colour.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(STATE_COLOURS.get(state, STATE_COLOURS[SessionState.IDLE])))
    inset = size * 0.02
    painter.drawEllipse(QRectF(inset, inset, size - 2 * inset, size - 2 * inset))

    # Microphone knocked out in white; slightly inset so it never touches the disc edge.
    painter.setBrush(QColor("#ffffff"))
    painter.save()
    painter.translate(size * 0.5, size * 0.5)
    painter.scale(0.78, 0.78)
    painter.translate(-size * 0.5, -size * 0.5)
    painter.drawPath(_mic_path(size))
    painter.restore()
    painter.end()
    return pm


def state_icon(state: SessionState) -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(_pixmap(state, size))
    return icon


def app_icon() -> QIcon:
    return state_icon(SessionState.IDLE)


def all_state_icons() -> dict[SessionState, QIcon]:
    """Built once at startup: constructing a QIcon on every state change is wasteful and
    makes the tray flicker on some platforms."""
    return {state: state_icon(state) for state in SessionState}
