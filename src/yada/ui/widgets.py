"""Reusable settings widgets.

Three ideas recur across the tabs and are worth factoring out:

* **Model pickers are editable.** Discovery ranks and suggests; it never gates. A model that
  shipped this morning must be usable this morning, so free text is always accepted.
* **Discovery has a visible age.** Every picker carries a line saying when its list came
  from and why a refresh failed, so "why isn't the new model here" has an answer on screen.
* **Capability options say how sure they are.** A tri-state option renders as a definite
  label, a "try to" label, or a disabled one, with the billing caveat in the tooltip.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..providers.base import (
    PRIORITY_LABELS,
    PRIORITY_TOOLTIPS,
    REASONING_LABELS,
    REASONING_TOOLTIPS,
    ModelInfo,
    Support,
)


def hint(text: str) -> QLabel:
    """Small, wrapped, dimmed explanatory text."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(mid); font-size: 11px;")
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return label


class ModelPicker(QWidget):
    """Editable model combo plus a discovery-age line and a refresh button."""

    refresh_requested = Signal()
    changed = Signal(str)

    def __init__(self, *, allow_auto: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._allow_auto = allow_auto

        self.combo = QComboBox()
        self.combo.setEditable(True)  # free text always permitted
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.setMinimumWidth(280)
        self.combo.currentTextChanged.connect(self._on_changed)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Ask the provider which models it currently offers.")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)

        self.status = hint("Models not discovered yet.")
        self.drift = hint("")
        self.drift.setStyleSheet("color: #c07000; font-size: 11px;")
        self.drift.hide()

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.combo, 1)
        row.addWidget(self.refresh_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(row)
        layout.addWidget(self.status)
        layout.addWidget(self.drift)

    def _on_changed(self, text: str) -> None:
        self.changed.emit("" if text == self.AUTO_LABEL else text)

    AUTO_LABEL = "Automatic (newest available)"

    def set_models(self, models: Iterable[ModelInfo], *, current: str) -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        if self._allow_auto:
            self.combo.addItem(self.AUTO_LABEL, "")
        for m in models:
            label = m.display
            if m.input_cost_per_mtok is not None:
                label += f"   (${m.input_cost_per_mtok:.2f}/M in)"
            self.combo.addItem(label, m.id)
        # A pinned model that discovery did not return is still selectable -- it may be new,
        # or discovery may have failed.
        if current and self.combo.findData(current) < 0:
            self.combo.addItem(f"{current}   (not in the discovered list)", current)
        index = self.combo.findData(current)
        self.combo.setCurrentIndex(index if index >= 0 else 0)
        self.combo.blockSignals(False)

    def current_model(self) -> str:
        data = self.combo.currentData()
        if data is not None:
            return str(data)
        text = self.combo.currentText().strip()
        return "" if text == self.AUTO_LABEL else text

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_drift_warning(self, text: str | None) -> None:
        self.drift.setText(text or "")
        self.drift.setVisible(bool(text))


class SupportCheckBox(QCheckBox):
    """A checkbox whose wording depends on how sure we are the option works.

    The middle state is the interesting one: rather than hiding an option that might work or
    implying a guarantee, it says "try to" and puts the cost caveat in the tooltip.
    """

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind
        self.set_support(Support.UNKNOWN)

    def set_support(self, support: Support) -> None:
        labels = PRIORITY_LABELS if self._kind == "priority" else REASONING_LABELS
        tips = PRIORITY_TOOLTIPS if self._kind == "priority" else REASONING_TOOLTIPS
        self.setText(labels[support])
        self.setToolTip(tips[support])
        self.setEnabled(support is not Support.UNSUPPORTED)
        if support is Support.UNSUPPORTED:
            self.setChecked(False)


class StringListEditor(QWidget):
    """Edit a list of short strings. Used for the vocabulary terms.

    Deliberately one-per-row rather than a comma-separated box: terms can contain commas,
    and the provider's keyword field wants one term per line anyway.
    """

    changed = Signal()

    def __init__(self, *, placeholder: str = "Add a term…", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.setAlternatingRowColors(True)
        self.list.itemChanged.connect(lambda _: self.changed.emit())

        self.entry = QLineEdit()
        self.entry.setPlaceholderText(placeholder)
        self.entry.returnPressed.connect(self._add)

        add = QPushButton("Add")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.entry, 1)
        row.addWidget(add)
        row.addWidget(remove)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list, 1)
        layout.addLayout(row)

    def _add(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        if any(self.list.item(i).text() == text for i in range(self.list.count())):
            self.entry.clear()
            return
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.list.addItem(item)
        self.entry.clear()
        self.changed.emit()

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.changed.emit()

    def set_values(self, values: Iterable[str]) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for value in values:
            item = QListWidgetItem(value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.list.addItem(item)
        self.list.blockSignals(False)

    def values(self) -> list[str]:
        out = []
        for i in range(self.list.count()):
            text = self.list.item(i).text().strip()
            if text:
                out.append(text)
        return out


class PromptEditor(QPlainTextEdit):
    """Multi-line prompt field with a sane default height."""

    def __init__(self, placeholder: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setTabChangesFocus(True)
        self.setMinimumHeight(90)


def labelled(text: str, widget: QWidget, *, tip: str = "") -> QWidget:
    """A caption above a widget, with an optional hint below."""
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    caption = QLabel(text)
    caption.setStyleSheet("font-weight: 600;")
    layout.addWidget(caption)
    layout.addWidget(widget)
    if tip:
        layout.addWidget(hint(tip))
    return container


def button_row(*buttons: QPushButton, stretch_first: bool = True) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    if stretch_first:
        row.addStretch(1)
    for b in buttons:
        row.addWidget(b)
    return row


def wire(widget: QWidget, callback: Callable[[], None]) -> None:
    """Connect a widget's most relevant 'value changed' signal to `callback`.

    Saves repeating a signal-name lookup per field across seven tabs.
    """
    for name in ("currentTextChanged", "textChanged", "toggled", "valueChanged", "changed"):
        signal = getattr(widget, name, None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(lambda *_: callback())
            return
