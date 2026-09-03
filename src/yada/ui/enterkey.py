"""Enter activates the focused button.

Qt does not do this outside a dialog. `QPushButton` handles Space, and Return only when it
is a dialog's default button -- so in yada's settings window a button could be focused, look
focused, and do nothing at all when Enter was pressed. That is not a defensible thing for a
keyboard user to discover.

Installed application-wide rather than by walking the widget tree, for the same reason as
the wheel guard: the transform steps editor and the sound library build their rows on the
fly, and a one-off pass over existing buttons would miss exactly those.

Only a focused *button* is affected. Enter belongs to whatever has focus otherwise -- a
line edit's `returnPressed` (the vocabulary term box relies on it), a multi-line prompt
editor where Enter inserts a newline, and the shortcut capture field, which has to be able
to record Enter as part of a shortcut.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QPushButton, QToolButton

# Buttons a press should activate. Deliberately not QAbstractButton: toggling a checkbox
# or a radio button with Enter is not a convention anyone expects, and Space already does it.
ACTIVATED = (QPushButton, QToolButton)

_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)


class EnterActivates(QObject):
    """Application-wide event filter. Install once, on the QApplication."""

    def eventFilter(self, watched, event) -> bool:  # Qt naming convention
        if event.type() != QEvent.Type.KeyPress:
            return False
        if event.key() not in _KEYS:
            return False
        # A modifier means something else is being asked for -- Ctrl+Enter and friends
        # belong to whatever defines them, not to a button.
        if event.modifiers() not in (
            Qt.KeyboardModifier.NoModifier,
            Qt.KeyboardModifier.KeypadModifier,
        ):
            return False
        if not isinstance(watched, ACTIVATED):
            return False
        if not watched.hasFocus() or not watched.isEnabled():
            return False
        watched.click()
        return True


def install(app) -> EnterActivates:
    """Install the filter and return it, so the caller can keep it alive.

    Qt does not take ownership of an event filter, so one that is not referenced anywhere
    is garbage collected and silently stops working.
    """
    guard = EnterActivates(app)
    app.installEventFilter(guard)
    return guard
