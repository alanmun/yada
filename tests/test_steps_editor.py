"""Editing cleanup steps must not disturb the field being edited."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from yada.config import TransformStep
from yada.ui.steps_editor import StepsEditor


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_typing_in_model_instructions_keeps_the_caret_in_place(app):
    editor = StepsEditor()
    editor.set_steps([TransformStep(system_prompt="abcdef")])
    cursor = editor.system_prompt.textCursor()
    cursor.setPosition(3)
    editor.system_prompt.setTextCursor(cursor)

    editor.system_prompt.insertPlainText("X")

    assert editor.system_prompt.toPlainText() == "abcXdef"
    assert editor.system_prompt.textCursor().position() == 4
