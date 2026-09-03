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
from typing import ClassVar

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QPainter,
    QPalette,
    QPen,
    QStandardItem,
    QStandardItemModel,
)
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


class HintLabel(QLabel):
    """Explanatory text that stays readable in light *and* dark themes.

    The first version of this styled itself `color: palette(mid); font-size: 11px`. On a
    light Windows theme that reads fine. On dark it is low-contrast grey on grey and
    genuinely hard to read, and 11px is roughly 8pt on Windows -- small on top of dim.

    So the colour is computed from the live palette instead of named from it: blend the
    window text colour toward the window background, which dims the text by a fixed
    proportion while keeping real contrast whichever way round those two colours are. The
    warning and error tones pick a light or dark variant based on background luminance for
    the same reason. Recomputed on palette change, so switching the OS theme while the
    window is open does not leave stale colours.
    """

    # Blend toward the background: clearly secondary, still comfortably legible.
    MUTED_BLEND = 0.30

    # (light-background variant, dark-background variant)
    _WARNING: ClassVar[tuple[str, str]] = ("#8a5300", "#f0b357")
    _ERROR: ClassVar[tuple[str, str]] = ("#b3261e", "#ff8a80")

    def __init__(self, text: str = "", *, tone: str = "muted", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._tone = tone
        # setStyleSheet emits a PaletteChange, which re-enters changeEvent. Without this
        # guard the two call each other until the stack runs out.
        self._restyling = False
        self._applied: str | None = None
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._restyle()

    def _restyle(self) -> None:
        if self._restyling:
            return
        palette = self.palette()
        background = palette.color(QPalette.ColorRole.Window)
        if self._tone == "muted":
            colour = self._blend(
                palette.color(QPalette.ColorRole.WindowText), background, self.MUTED_BLEND
            )
        else:
            light_variant, dark_variant = self._WARNING if self._tone == "warning" else self._ERROR
            colour = QColor(dark_variant if _is_dark(background) else light_variant)
        # Only the colour is set. The font size is left alone deliberately: dimmed text is
        # already secondary, and shrinking it as well made it unreadable.
        name = colour.name()
        if name == self._applied:
            return
        self._restyling = True
        try:
            self._applied = name
            self.setStyleSheet(f"color: {name};")
        finally:
            self._restyling = False

    @staticmethod
    def _blend(foreground: QColor, background: QColor, amount: float) -> QColor:
        keep = 1.0 - amount
        return QColor(
            round(foreground.red() * keep + background.red() * amount),
            round(foreground.green() * keep + background.green() * amount),
            round(foreground.blue() * keep + background.blue() * amount),
        )

    def changeEvent(self, event) -> None:  # Qt naming convention
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ThemeChange):
            self._restyle()
        super().changeEvent(event)


def _is_dark(colour: QColor) -> bool:
    """Perceptual luminance, so 'dark' matches what the eye reports rather than raw RGB."""
    r, g, b = colour.redF(), colour.greenF(), colour.blueF()
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 0.5


def hint(text: str) -> QLabel:
    """Wrapped, dimmed explanatory text that is legible in either theme."""
    return HintLabel(text)


def warning_label(text: str = "") -> QLabel:
    return HintLabel(text, tone="warning")


def error_label(text: str = "") -> QLabel:
    return HintLabel(text, tone="error")


class ModelPicker(QWidget):
    """Editable model combo plus a discovery-age line and a refresh button."""

    refresh_requested = Signal()
    changed = Signal(str)

    def __init__(self, *, allow_auto: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._allow_auto = allow_auto
        # What settings say, remembered independently of the list. Discovery arrives after
        # the window opens, and without this the combo is empty in between -- so the
        # autosave that follows any edit wrote that emptiness back over the user's model.
        self._configured = ""
        # A repopulation that arrived while the popup was open, held until it closes.
        self._pending: tuple[list[ModelInfo], str, str] | None = None

        self.combo = QComboBox()
        self.combo.setEditable(True)  # free text always permitted
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.setMinimumWidth(280)
        self.combo.currentTextChanged.connect(self._on_changed)
        # The popup closing is the moment a deferred refresh becomes safe to apply.
        self.combo.view().installEventFilter(self)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Ask the provider which models it currently offers.")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)

        self.status = hint("Models not discovered yet.")
        self.drift = warning_label("")
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

    def set_current(self, model: str) -> None:
        """Show the configured model before discovery has produced anything to choose from.

        Called on load. The list arrives later, so until it does the combo would otherwise
        be empty and `current_model()` would report nothing -- which is how a configured
        transform model was silently replaced by whatever ended up first in the list.
        """
        self._configured = model or ""
        self.combo.blockSignals(True)
        if model and self.combo.findData(model) < 0:
            self.combo.addItem(model, model)
        index = self.combo.findData(model)
        if index >= 0:
            self.combo.setCurrentIndex(index)
        elif self._allow_auto:
            self.combo.setCurrentIndex(0)
        self.combo.blockSignals(False)

    def eventFilter(self, watched, event) -> bool:  # Qt naming convention
        if (
            watched is self.combo.view()
            and event.type() == QEvent.Type.Hide
            and self._pending is not None
        ):
            models, current, recommended = self._pending
            self._pending = None
            self._apply_models(models, current=current, recommended=recommended)
        return super().eventFilter(watched, event)

    def set_models(
        self, models: Iterable[ModelInfo], *, current: str, recommended: str = ""
    ) -> None:
        """Replace the list, unless the user is currently looking at it.

        Rebuilding an open popup destroys the item under the pointer, so a click lands on
        nothing. Discovery finishing, or any other background refresh, must not decide the
        moment a selection is being made -- it waits for the popup to close.
        """
        models = list(models)
        self._configured = current or self._configured
        if self.combo.view().isVisible():
            self._pending = (models, current, recommended)
            return
        self._apply_models(models, current=current, recommended=recommended)

    def _apply_models(
        self, models: list[ModelInfo], *, current: str, recommended: str = ""
    ) -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        if self._allow_auto:
            self.combo.addItem(self.AUTO_LABEL, "")
        for m in models:
            label = m.display
            if m.input_cost_per_mtok is not None:
                label += f"   (${m.input_cost_per_mtok:.2f}/M in)"
            if m.id == recommended:
                # Marked rather than reordered: the list stays newest-first, which is what
                # makes a new release visible, and the curated pick is still findable.
                label += "   ★ recommended"
            self.combo.addItem(label, m.id)
        # A pinned model that discovery did not return is still selectable -- it may be new,
        # or discovery may have failed.
        if current and self.combo.findData(current) < 0:
            self.combo.addItem(f"{current}   (not in the discovered list)", current)
        index = self.combo.findData(current)
        if index < 0 and recommended:
            # Nothing chosen yet: start on the curated pick rather than on whatever
            # happens to sort first, which is how a picker with no "automatic" entry
            # silently selected the newest unrelated model.
            index = self.combo.findData(recommended)
        self.combo.setCurrentIndex(index if index >= 0 else 0)
        self.combo.blockSignals(False)

    def current_model(self) -> str:
        if self.combo.count() == 0:
            # Nothing to choose from yet. Reporting "" here is what let an autosave
            # overwrite a perfectly good setting with nothing.
            return self._configured
        data = self.combo.currentData()
        if data is not None:
            return str(data)
        text = self.combo.currentText().strip()
        return "" if text == self.AUTO_LABEL else text

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_drift_warning(self, text: str | None) -> None:
        """Show a drift warning, unless it would just repeat the status line.

        "Models not discovered yet." and "No models discovered yet for this provider"
        were both being shown, one under the other, saying the same thing twice.
        """
        if text and self.status.text().strip().rstrip(".") in text.rstrip("."):
            text = None
        self.drift.setText(text or "")
        self.drift.setVisible(bool(text))


class LevelMeter(QWidget):
    """Live input level, so the gain dial stops being a blind one.

    Peak with a slow-falling hold rather than a bar that tracks the signal exactly: a clip
    lasts a couple of milliseconds and would otherwise be gone before it could be seen, and
    clipping is the thing that actually ruins a transcript.
    """

    TICK_MS = 50
    LEVEL_DECAY = 0.25
    PEAK_DECAY = 0.02
    # Above this the input is close enough to full scale to be worth warning about.
    HOT = 0.80
    CLIPPING = 0.97

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._level = 0.0
        self._peak = 0.0
        self._active = False
        self.setMinimumHeight(max(12, round(self.fontMetrics().height() * 0.75)))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._decay)

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self._timer.start()
        else:
            self._timer.stop()
            self._level = self._peak = 0.0
        self.update()

    def set_level(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        self._level = max(self._level, level)
        self._peak = max(self._peak, level)
        self.update()

    def _decay(self) -> None:
        self._level = max(0.0, self._level - self.LEVEL_DECAY)
        self._peak = max(self._level, self._peak - self.PEAK_DECAY)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette = self.palette()
        rect = self.rect().adjusted(0, 0, -1, -1)
        radius = rect.height() / 2

        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1))
        painter.setBrush(palette.color(QPalette.ColorRole.Base))
        painter.drawRoundedRect(rect, radius, radius)

        if not self._active or self._level <= 0.0:
            return

        filled = QRectF(rect)
        filled.setWidth(rect.width() * self._level)
        if self._level >= self.CLIPPING:
            colour = QColor("#e05252")
        elif self._level >= self.HOT:
            colour = QColor("#e0a132")
        else:
            colour = palette.color(QPalette.ColorRole.Highlight)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawRoundedRect(filled, radius, radius)

        # The held peak, drawn as a thin line so a transient clip stays readable.
        if self._peak > 0.01:
            x = rect.left() + rect.width() * self._peak
            painter.setPen(QPen(palette.color(QPalette.ColorRole.BrightText), 2))
            painter.drawLine(int(x), rect.top() + 2, int(x), rect.bottom() - 1)


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


class CheckableComboBox(QComboBox):
    """A dropdown whose items are checkboxes, summarising the selection when closed.

    Qt has no multi-select combo, and the obvious workarounds are worse: a comma-separated
    text field expects people to know ISO codes, and a modal dialog is heavy for choosing
    a couple of languages. So the popup's viewport is filtered to toggle a row's check
    state on click *without* closing, which is the behaviour people expect from this
    control everywhere else.
    """

    selection_changed = Signal()

    def __init__(self, *, empty_text: str = "None", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._empty_text = empty_text
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText(empty_text)
        # Without this the line edit shows the highlighted row rather than the summary.
        self.lineEdit().installEventFilter(self)
        self.view().viewport().installEventFilter(self)
        self._model.itemChanged.connect(self._on_item_changed)

    def showPopup(self) -> None:  # Qt naming convention
        """Size the popup from its own rows before opening it.

        The explicit item hints in `set_options` are correct and were not enough. Inside
        the settings window Qt opened this popup as a six-pixel sliver with a zero-height
        viewport -- while every row reported 38 pixels -- which reads as "the language list
        is empty". That was reported twice, and the first fix was verified only on Linux,
        where it never reproduced.

        Measured on Windows: clearing the application stylesheet fixed it, and re-applying
        the *same* stylesheet also fixed it. So the container's cached geometry was the
        problem rather than any rule. Re-polishing every widget in the app in order to open
        a combo is not a fix; taking the height from the rows is, and it cannot be undone
        by whatever a platform style decides to cache.
        """
        view = self.view()
        rows = min(self.count(), self.maxVisibleItems())
        row_height = view.sizeHintForRow(0) if self.count() else 0
        if rows > 0 and row_height > 0:
            view.setMinimumHeight(rows * row_height + 2 * view.frameWidth())
        super().showPopup()

    # -- items --------------------------------------------------------------------------

    def set_options(self, options: list[tuple[str, str]], *, checked: list[str]) -> None:
        """`options` is [(value, label)]; `checked` the values to tick."""
        # An explicit row height, rather than relying on the stylesheet. 0.1.7 styled
        # QComboBox without styling its popup view, and the dropdown opened with no
        # visible rows at all -- it read as "no options". Setting the hint from the font
        # makes the height independent of how any platform style interprets the QSS.
        row_height = QFontMetrics(self.font()).height() + 12

        self._model.blockSignals(True)
        self._model.clear()
        wanted = set(checked)
        for value, label in options:
            item = QStandardItem(label)
            item.setData(value, Qt.ItemDataRole.UserRole)
            item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            item.setSizeHint(QSize(0, row_height))
            item.setCheckState(
                Qt.CheckState.Checked if value in wanted else Qt.CheckState.Unchecked
            )
            self._model.appendRow(item)
        self._model.blockSignals(False)
        # Show a decent number of rows before scrolling, rather than Qt's default of ten
        # at a tiny height.
        self.setMaxVisibleItems(12)
        self._refresh_text()

    def checked_values(self) -> list[str]:
        out = []
        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return out

    # -- behaviour ----------------------------------------------------------------------

    def eventFilter(self, watched, event) -> bool:  # Qt naming convention
        if event.type() == QEvent.Type.MouseButtonRelease:
            if watched is self.view().viewport():
                index = self.view().indexAt(event.pos())
                if index.isValid():
                    item = self._model.itemFromIndex(index)
                    item.setCheckState(
                        Qt.CheckState.Unchecked
                        if item.checkState() == Qt.CheckState.Checked
                        else Qt.CheckState.Checked
                    )
                # Consume the click so the popup stays open for the next tick.
                return True
            if watched is self.lineEdit():
                self.showPopup()
                return True
        return super().eventFilter(watched, event)

    def _on_item_changed(self, _item) -> None:
        self._refresh_text()
        self.selection_changed.emit()

    def _refresh_text(self) -> None:
        labels = [
            self._model.item(row).text()
            for row in range(self._model.rowCount())
            if self._model.item(row).checkState() == Qt.CheckState.Checked
        ]
        # Strip the parenthesised endonym for the summary; the full label is in the list.
        short = [label.split("  (")[0] for label in labels]
        if not short:
            self.lineEdit().setText("")
        elif len(short) <= 3:
            self.lineEdit().setText(", ".join(short))
        else:
            self.lineEdit().setText(f"{', '.join(short[:3])} and {len(short) - 3} more")
