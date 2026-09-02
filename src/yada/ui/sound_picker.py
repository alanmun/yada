"""Chime selection, with your own sounds sitting alongside the built-in ones.

The two are deliberately indistinguishable in the pickers: one list, built-ins first, so
switching between a default and an import is a single click and neither feels like the
special case. Imports are managed separately below, because that is where removal belongs --
a delete button on every row of a dropdown you only open to choose something would be a
mis-click waiting to happen.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..output import sounds
from .widgets import hint


def _describe(sound: sounds.Sound) -> str:
    duration = sound.duration_seconds()
    if duration is None:
        return sound.name
    if duration > sounds.LONG_SOUND_SECONDS:
        return f"{sound.name}  ({duration:.1f}s — long for a chime)"
    return f"{sound.name}  ({duration:.1f}s)"


class ChimeRow(QWidget):
    """One stage: whether it chimes, which sound, and a way to hear it."""

    changed = Signal()
    preview_requested = Signal(str)

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.enabled = QCheckBox(label)
        self.enabled.toggled.connect(self._on_toggled)
        self.enabled.toggled.connect(lambda _: self.changed.emit())

        self.combo = QComboBox()
        self.combo.setMinimumWidth(260)
        self.combo.currentIndexChanged.connect(lambda _: self.changed.emit())

        self.preview = QPushButton("Preview")
        self.preview.setToolTip("Play this sound now.")
        self.preview.clicked.connect(self._on_preview)

        row = QHBoxLayout()
        row.setContentsMargins(22, 0, 0, 0)  # indent under the checkbox
        row.addWidget(self.combo, 1)
        row.addWidget(self.preview)
        picker = QWidget()
        picker.setLayout(row)
        self._picker = picker

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.enabled)
        layout.addWidget(picker)

    def _on_toggled(self, on: bool) -> None:
        # A sound picker for a chime that is switched off is just noise on the page.
        self._picker.setEnabled(on)

    def _on_preview(self) -> None:
        if sound_id := self.current_sound():
            self.preview_requested.emit(sound_id)

    def set_library(self, library: list[sounds.Sound], *, current: str) -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        for sound in library:
            self.combo.addItem(_describe(sound), sound.id)
        index = self.combo.findData(current)
        if index < 0 and current:
            # The selection has gone missing -- an import that was deleted, or a config
            # brought from another machine. Say so rather than silently reselecting.
            self.combo.addItem(f"{current.split(':', 1)[-1]}  (missing)", current)
            index = self.combo.count() - 1
        self.combo.setCurrentIndex(max(index, 0))
        self.combo.blockSignals(False)

    def current_sound(self) -> str:
        data = self.combo.currentData()
        return str(data) if data else ""

    def set_enabled_state(self, on: bool) -> None:
        self.enabled.setChecked(on)
        self._picker.setEnabled(on)

    def is_enabled(self) -> bool:
        return self.enabled.isChecked()


class SoundLibraryEditor(QWidget):
    """Your imported sounds, each with an X to remove it, plus an import button."""

    library_changed = Signal()
    preview_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows = QVBoxLayout()
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(2)

        self._empty = hint(
            "No sounds of your own yet. Import a WAV, MP3, OGG, FLAC or M4A and it will "
            "appear in both pickers above."
        )

        self.import_button = QPushButton("Import sound…")
        self.import_button.clicked.connect(self._on_import)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._empty)
        layout.addLayout(self._rows)
        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 0)
        row.addWidget(self.import_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh()

    # -- rows ---------------------------------------------------------------------------

    def refresh(self) -> None:
        while self._rows.count():
            item = self._rows.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()

        custom = sounds.custom_sounds()
        self._empty.setVisible(not custom)
        for sound in custom:
            self._rows.addWidget(self._build_row(sound))

    def _build_row(self, sound: sounds.Sound) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.NoFrame)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)

        label = QLabel(_describe(sound))
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(label, 1)

        play = QPushButton("Preview")
        play.clicked.connect(lambda _=False, sid=sound.id: self.preview_requested.emit(sid))
        layout.addWidget(play)

        remove = QPushButton("✕")
        remove.setFixedSize(QSize(28, 28))
        remove.setToolTip(f"Remove “{sound.name}” from yada")
        remove.clicked.connect(lambda _=False, s=sound: self._on_remove(s))
        layout.addWidget(remove)
        return frame

    # -- actions ------------------------------------------------------------------------

    def _on_import(self) -> None:
        patterns = " ".join(f"*{suffix}" for suffix in sounds.IMPORTABLE_SUFFIXES)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a sound", "", f"Audio files ({patterns});;All files (*)"
        )
        if not path:
            return
        try:
            imported = sounds.import_sound(Path(path))
        except sounds.SoundError as exc:
            QMessageBox.warning(self, "Could not import that sound", str(exc))
            return
        self.refresh()
        self.library_changed.emit()
        self.preview_requested.emit(imported.id)

    def _on_remove(self, sound: sounds.Sound) -> None:
        confirm = QMessageBox.question(
            self,
            "Remove sound",
            f"Remove “{sound.name}” from yada?\n\n"
            "The file is deleted from yada's own folder. Anything still using it falls "
            "back to the built-in chime.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if not sounds.remove_sound(sound.id):
            QMessageBox.warning(self, "Could not remove", "That file could not be deleted.")
            return
        self.refresh()
        self.library_changed.emit()


class VolumeRow(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(60)
        self.slider.valueChanged.connect(lambda _: self.changed.emit())
        self._readout = QLabel("60%")
        self._readout.setMinimumWidth(44)
        self.slider.valueChanged.connect(lambda v: self._readout.setText(f"{v}%"))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Volume"))
        layout.addWidget(self.slider, 1)
        layout.addWidget(self._readout)

    def value(self) -> float:
        return self.slider.value() / 100.0

    def set_value(self, volume: float) -> None:
        self.slider.setValue(round(max(0.0, min(1.0, volume)) * 100))
