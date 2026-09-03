"""Enter activates a focused button.

Qt does not do this outside a dialog: QPushButton handles Space, and Return only when it is
a dialog's default button. So in yada's settings window a button could be focused, look
focused, and do nothing at all when Enter was pressed. Measured: two Return presses on a
focused button produced zero clicks, and Space produced one.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from yada.ui import enterkey


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    if not getattr(app, "_test_enter_guard", None):
        app._test_enter_guard = enterkey.install(app)
    return app


def _press(qapp, widget, key=Qt.Key.Key_Return, modifiers=Qt.KeyboardModifier.NoModifier):
    qapp.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))
    qapp.processEvents()


def _focused(qapp, widget):
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.addWidget(widget)
    host.show()
    qapp.processEvents()
    widget.setFocus()
    qapp.processEvents()
    return host


def test_enter_clicks_a_focused_button(qapp):
    button = QPushButton("Go")
    clicks = []
    button.clicked.connect(lambda: clicks.append(1))
    host = _focused(qapp, button)
    assert host is not None
    if not button.hasFocus():
        pytest.skip("this platform will not focus an offscreen button")

    _press(qapp, button)
    assert clicks == [1]

    _press(qapp, button, key=Qt.Key.Key_Enter)  # the keypad one
    assert clicks == [1, 1]


def test_a_disabled_button_is_not_clicked(qapp):
    button = QPushButton("Go")
    button.setEnabled(False)
    clicks = []
    button.clicked.connect(lambda: clicks.append(1))
    host = _focused(qapp, button)
    assert host is not None
    _press(qapp, button)
    assert clicks == []


def test_enter_is_left_alone_in_a_line_edit(qapp):
    """The vocabulary term box adds a term on returnPressed; hijacking Enter would break it."""
    field = QLineEdit()
    returns = []
    field.returnPressed.connect(lambda: returns.append(1))
    host = _focused(qapp, field)
    assert host is not None
    if not field.hasFocus():
        pytest.skip("this platform will not focus an offscreen field")

    _press(qapp, field)
    assert returns == [1], "the field's own handler must still run"


def test_enter_still_inserts_a_newline_in_a_text_editor(qapp):
    editor = QPlainTextEdit()
    host = _focused(qapp, editor)
    assert host is not None
    if not editor.hasFocus():
        pytest.skip("this platform will not focus an offscreen editor")

    _press(qapp, editor)
    assert "\n" in editor.toPlainText(), "a multi-line editor owns its own Enter"


def test_a_checkbox_is_not_toggled_by_enter(qapp):
    """Space already does that, and Enter toggling a checkbox is nobody's convention."""
    box = QCheckBox("Thing")
    host = _focused(qapp, box)
    assert host is not None
    _press(qapp, box)
    assert box.isChecked() is False


def test_a_modified_enter_is_not_a_click(qapp):
    """Ctrl+Enter belongs to whatever defines it, not to whichever button has focus."""
    button = QPushButton("Go")
    clicks = []
    button.clicked.connect(lambda: clicks.append(1))
    host = _focused(qapp, button)
    assert host is not None
    if not button.hasFocus():
        pytest.skip("this platform will not focus an offscreen button")

    _press(qapp, button, modifiers=Qt.KeyboardModifier.ControlModifier)
    assert clicks == []


def test_only_buttons_are_covered():
    """Named explicitly, so widening it is a deliberate decision."""
    from PySide6.QtWidgets import QAbstractButton, QToolButton

    assert QPushButton in enterkey.ACTIVATED
    assert QToolButton in enterkey.ACTIVATED
    assert QAbstractButton not in enterkey.ACTIVATED
