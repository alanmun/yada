"""Scrolling a settings page must not rewrite the settings.

Qt's default wheel handling on combos, spin boxes and sliders fires whenever the pointer is
over them, focused or not. On a scrolling page that silently corrupts values as the user
moves down it, with nothing to indicate anything changed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from yada.ui import wheelguard


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    if not getattr(app, "_test_guard", None):
        app._test_guard = wheelguard.install(app)
    return app


def _wheel(widget, delta: int = -120) -> QWheelEvent:
    centre = QPointF(widget.rect().center())
    return QWheelEvent(
        centre,
        QPointF(widget.mapToGlobal(widget.rect().center())),
        QPoint(0, delta),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def _page(qapp, control):
    """A control inside a scroll area, as every settings control is."""
    inner = QWidget()
    layout = QVBoxLayout(inner)
    layout.addWidget(control)
    for _ in range(40):  # make the page genuinely taller than the viewport
        layout.addWidget(QWidget())
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(inner)
    area.resize(300, 120)
    area.show()
    qapp.processEvents()
    # Qt focuses the first focusable widget in a shown window, so the control under test
    # would arrive already focused -- which is the one case the guard deliberately allows.
    control.clearFocus()
    qapp.processEvents()
    return area


def test_scrolling_over_an_unfocused_combo_does_not_change_it(qapp):
    combo = QComboBox()
    combo.addItems(["one", "two", "three"])
    combo.setCurrentIndex(0)
    area = _page(qapp, combo)  # kept: it owns the widgets
    assert area is not None

    QApplication.sendEvent(combo, _wheel(combo))
    assert combo.currentIndex() == 0, "an unfocused combo must ignore the wheel"


def test_scrolling_over_an_unfocused_spinbox_does_not_change_it(qapp):
    spin = QDoubleSpinBox()
    spin.setRange(0.1, 4.0)
    spin.setValue(1.0)
    area = _page(qapp, spin)  # kept: it owns the widgets
    assert area is not None

    QApplication.sendEvent(spin, _wheel(spin))
    assert spin.value() == 1.0, "input gain must not drift when scrolling past it"


def test_scrolling_over_an_unfocused_slider_does_not_change_it(qapp):
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(60)
    area = _page(qapp, slider)  # kept: it owns the widgets
    assert area is not None

    QApplication.sendEvent(slider, _wheel(slider))
    assert slider.value() == 60, "chime volume must not drift when scrolling past it"


def test_a_focused_control_still_responds(qapp):
    """Deliberate adjustment with the wheel must keep working."""
    combo = QComboBox()
    combo.addItems(["one", "two", "three"])
    combo.setCurrentIndex(0)
    area = _page(qapp, combo)  # kept: it owns the widgets
    assert area is not None
    combo.setFocus()
    qapp.processEvents()
    if not combo.hasFocus():
        pytest.skip("this platform will not give focus to an offscreen widget")

    QApplication.sendEvent(combo, _wheel(combo))
    assert combo.currentIndex() == 1, "a focused combo should still accept the wheel"


def test_scrollbars_keep_their_wheel(qapp):
    """A scroll bar exists to scroll; guarding it would freeze the page."""
    area = _page(qapp, QComboBox())
    bar = area.verticalScrollBar()
    assert isinstance(bar, wheelguard.EXEMPT), "scroll bars must be exempt"


def test_the_guard_covers_every_value_control_used_in_settings():
    """Named explicitly, so a new control type is a deliberate decision."""
    from PySide6.QtWidgets import QAbstractSlider, QAbstractSpinBox

    assert QComboBox in wheelguard.GUARDED
    assert QAbstractSpinBox in wheelguard.GUARDED
    assert QAbstractSlider in wheelguard.GUARDED
