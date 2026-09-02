"""Editor for the ordered transform pipeline.

A list on the left, an editor for the selected step on the right. Order matters and is
editable, because the useful arrangement is usually "fix my known misspellings
deterministically, *then* let a model tidy the grammar" -- doing it the other way round lets
the model reintroduce the very spellings the find/replace exists to fix.
"""

from __future__ import annotations

import dataclasses
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import TransformStep
from ..pipeline.transform import DEFAULT_SYSTEM_PROMPT
from .widgets import PromptEditor, error_label, hint, labelled

STEP_LABELS = {
    "prompt_transform": "AI cleanup",
    "find_replace": "Find and replace",
}


class StepsEditor(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._steps: list[TransformStep] = []
        self._loading = False

        self.list = QListWidget()
        self.list.setMaximumWidth(220)
        self.list.currentRowChanged.connect(self._on_selection)

        add_prompt = QPushButton("+ AI cleanup")
        add_prompt.clicked.connect(lambda: self._add("prompt_transform"))
        add_replace = QPushButton("+ Find/replace")
        add_replace.clicked.connect(lambda: self._add("find_replace"))
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        self.up_button = QPushButton("↑")
        self.up_button.setFixedWidth(32)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("↓")
        self.down_button.setFixedWidth(32)
        self.down_button.clicked.connect(lambda: self._move(1))

        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.addWidget(add_prompt)
        add_row.addWidget(add_replace)
        order_row = QHBoxLayout()
        order_row.setContentsMargins(0, 0, 0, 0)
        order_row.addWidget(self.up_button)
        order_row.addWidget(self.down_button)
        order_row.addWidget(self.remove_button, 1)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(self.list, 1)
        left.addLayout(add_row)
        left.addLayout(order_row)

        # -- prompt_transform pane
        self.enabled_box = QCheckBox("Run this step")
        self.enabled_box.toggled.connect(self._collect)
        self.system_prompt = PromptEditor(DEFAULT_SYSTEM_PROMPT)
        self.system_prompt.textChanged.connect(self._collect)
        self.user_template = QLineEdit()
        self.user_template.setPlaceholderText("{{input}}")
        self.user_template.textChanged.connect(self._collect)

        prompt_pane = QWidget()
        prompt_layout = QVBoxLayout(prompt_pane)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        prompt_layout.addWidget(self.enabled_box)
        prompt_layout.addWidget(
            labelled(
                "Instructions to the model",
                self.system_prompt,
                tip=(
                    "Your vocabulary terms are appended automatically. Use {{vocabulary}} to "
                    "place them somewhere specific instead."
                ),
            )
        )
        prompt_layout.addWidget(
            labelled(
                "Message template",
                self.user_template,
                tip="{{input}} is replaced with the text so far. Leave blank to send it as-is.",
            )
        )
        prompt_layout.addStretch(1)

        # -- find_replace pane
        self.fr_enabled = QCheckBox("Run this step")
        self.fr_enabled.toggled.connect(self._collect)
        self.find_field = QLineEdit()
        self.find_field.textChanged.connect(self._collect)
        self.replace_field = QLineEdit()
        self.replace_field.textChanged.connect(self._collect)
        self.regex_box = QCheckBox("Treat the search text as a regular expression")
        self.regex_box.toggled.connect(self._collect)
        # error_label picks a red that contrasts with the actual background; the previous
        # fixed #c0392b was unreadable on a dark theme.
        self.regex_error = error_label("")

        fr_pane = QWidget()
        fr_layout = QVBoxLayout(fr_pane)
        fr_layout.setContentsMargins(0, 0, 0, 0)
        fr_layout.addWidget(self.fr_enabled)
        fr_layout.addWidget(labelled("Find", self.find_field))
        fr_layout.addWidget(labelled("Replace with", self.replace_field))
        fr_layout.addWidget(self.regex_box)
        fr_layout.addWidget(self.regex_error)
        fr_layout.addWidget(
            hint(
                "Runs on your machine and costs nothing, so it is the reliable way to fix a "
                "spelling a model keeps getting wrong."
            )
        )
        fr_layout.addStretch(1)

        self.empty_pane = QWidget()
        empty_layout = QVBoxLayout(self.empty_pane)
        empty_layout.addWidget(
            hint("Add a step to build a cleanup pipeline. Steps run top to bottom.")
        )
        empty_layout.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_pane)  # 0
        self.stack.addWidget(prompt_pane)  # 1
        self.stack.addWidget(fr_pane)  # 2

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(left)
        layout.addWidget(self.stack, 1)
        self._refresh_buttons()

    # -- data ---------------------------------------------------------------------------

    def set_steps(self, steps: list[TransformStep]) -> None:
        # Copy, so editing here does not mutate live settings before the user saves.
        # dataclasses.replace rather than vars(): TransformStep uses slots and has no __dict__.
        self._steps = [dataclasses.replace(s) for s in steps]
        self._rebuild_list()
        if self._steps:
            self.list.setCurrentRow(0)
        else:
            self.stack.setCurrentIndex(0)
        self._refresh_buttons()

    def steps(self) -> list[TransformStep]:
        return list(self._steps)

    # -- list -----------------------------------------------------------------------------

    def _rebuild_list(self) -> None:
        self.list.blockSignals(True)
        current = self.list.currentRow()
        self.list.clear()
        for index, step in enumerate(self._steps, start=1):
            label = f"{index}. {STEP_LABELS.get(step.type, step.type)}"
            if not step.enabled:
                label += "  (off)"
            item = QListWidgetItem(label)
            if not step.enabled:
                item.setForeground(Qt.GlobalColor.gray)
            self.list.addItem(item)
        self.list.blockSignals(False)
        if 0 <= current < self.list.count():
            self.list.setCurrentRow(current)

    def _add(self, kind: str) -> None:
        step = TransformStep(type=kind)
        if kind == "prompt_transform":
            step.system_prompt = DEFAULT_SYSTEM_PROMPT
        self._steps.append(step)
        self._rebuild_list()
        self.list.setCurrentRow(len(self._steps) - 1)
        self._refresh_buttons()
        self.changed.emit()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self._steps)):
            return
        del self._steps[row]
        self._rebuild_list()
        if self._steps:
            self.list.setCurrentRow(min(row, len(self._steps) - 1))
        else:
            self.stack.setCurrentIndex(0)
        self._refresh_buttons()
        self.changed.emit()

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        target = row + delta
        if not (0 <= row < len(self._steps) and 0 <= target < len(self._steps)):
            return
        self._steps[row], self._steps[target] = self._steps[target], self._steps[row]
        self._rebuild_list()
        self.list.setCurrentRow(target)
        self._refresh_buttons()
        self.changed.emit()

    def _refresh_buttons(self) -> None:
        row = self.list.currentRow()
        has = 0 <= row < len(self._steps)
        self.remove_button.setEnabled(has)
        self.up_button.setEnabled(has and row > 0)
        self.down_button.setEnabled(has and row < len(self._steps) - 1)

    # -- selection and editing ------------------------------------------------------------

    def _on_selection(self, row: int) -> None:
        self._refresh_buttons()
        if not (0 <= row < len(self._steps)):
            self.stack.setCurrentIndex(0)
            return
        step = self._steps[row]
        self._loading = True
        try:
            if step.type == "prompt_transform":
                self.enabled_box.setChecked(step.enabled)
                self.system_prompt.setPlainText(step.system_prompt)
                self.user_template.setText(step.user_prompt_template)
                self.stack.setCurrentIndex(1)
            else:
                self.fr_enabled.setChecked(step.enabled)
                self.find_field.setText(step.find)
                self.replace_field.setText(step.replace)
                self.regex_box.setChecked(step.use_regex)
                self.stack.setCurrentIndex(2)
        finally:
            self._loading = False
        self._validate_regex()

    def _collect(self) -> None:
        """Pull the visible fields back into the selected step."""
        if self._loading:
            return
        row = self.list.currentRow()
        if not (0 <= row < len(self._steps)):
            return
        step = self._steps[row]
        if step.type == "prompt_transform":
            step.enabled = self.enabled_box.isChecked()
            step.system_prompt = self.system_prompt.toPlainText()
            step.user_prompt_template = self.user_template.text()
        else:
            step.enabled = self.fr_enabled.isChecked()
            step.find = self.find_field.text()
            step.replace = self.replace_field.text()
            step.use_regex = self.regex_box.isChecked()
        self._rebuild_list()
        self._validate_regex()
        self.changed.emit()

    def _validate_regex(self) -> None:
        """Report a bad pattern here rather than at dictation time."""
        if self.stack.currentIndex() != 2 or not self.regex_box.isChecked():
            self.regex_error.setText("")
            return
        try:
            re.compile(self.find_field.text())
        except re.error as exc:
            self.regex_error.setText(f"Invalid regular expression: {exc}")
        else:
            self.regex_error.setText("")
