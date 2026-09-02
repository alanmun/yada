"""Stop unfocused controls from stealing the mouse wheel.

Qt gives QComboBox, QSpinBox, QDoubleSpinBox and QSlider a default wheel handler that
changes their value whenever the pointer is over them -- focused or not. Inside a scrolling
settings page that is destructive: scrolling past a dropdown silently rewrites it, and the
user has no reason to suspect anything changed. Settings can be corrupted by nothing more
than moving down the page.

Installed once on the QApplication rather than on individual widgets, so it also covers
controls built later -- the transform steps editor and the sound library rebuild their rows
on the fly, and a per-widget approach would miss exactly those.

The wheel is not merely swallowed. It is forwarded to the nearest scrolling ancestor, so
the page moves as the user intended.

Focus is not an exemption. An earlier version allowed the wheel on a focused control, on
the reasoning that clicking into something first makes the adjustment deliberate. In
practice a control stays focused long after you have stopped thinking about it, so
scrolling the page later still rewrote the value -- and the value it rewrote was one the
user had just set by hand. These controls are adjusted by clicking their arrows, dragging
them, or typing.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QScrollArea,
    QScrollBar,
    QWidget,
)

# Types whose value the wheel must not change while they are unfocused.
GUARDED = (QComboBox, QAbstractSpinBox, QAbstractSlider)

# Wheel events these need to keep: they exist to scroll. QScrollBar is a QAbstractSlider,
# and an item view is what a combo's popup is made of.
EXEMPT = (QScrollBar, QAbstractItemView)


def _scrolling_ancestor(widget: QWidget) -> QScrollArea | None:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


class WheelGuard(QObject):
    """Application-wide event filter. Install once, on the QApplication."""

    def eventFilter(self, watched, event) -> bool:  # Qt naming convention
        if event.type() != QEvent.Type.Wheel:
            return False
        if not isinstance(watched, GUARDED) or isinstance(watched, EXEMPT):
            return False
        # Give the scroll to the page instead of the control -- always. Focus used to be
        # an exemption here and it was one in name only: the control the user last touched
        # is still focused when they scroll past it.
        area = _scrolling_ancestor(watched)
        if area is not None:
            QApplication.sendEvent(area.viewport(), event)
        return True


def install(app) -> WheelGuard:
    """Install the guard and return it, so the caller can keep it alive.

    Qt does not take ownership of an event filter, so a filter that is not referenced
    anywhere is garbage collected and silently stops working.
    """
    guard = WheelGuard(app)
    app.installEventFilter(guard)
    return guard
