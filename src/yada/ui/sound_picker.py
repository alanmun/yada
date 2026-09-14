"""Chime selection, with your own sounds sitting alongside the built-in ones.

The two are deliberately indistinguishable in the pickers: one list, built-ins first, so
switching between a default and an import is a single click and neither feels like the
special case. Imports are managed separately below, because that is where removal belongs --
a delete button on every row of a dropdown you only open to choose something would be a
mis-click waiting to happen.

That management row is also where each import gets its own level. Files people already have
lying around are mastered anywhere from a whisper to full scale, so a single master volume
cannot make two of them sit at the same loudness -- turn it up for the quiet one and the
loud one becomes a jump scare. The per-sound slider is a trim relative to the master, it is
remembered per file, and moving it replays the sound at the new level, because the only
useful answer to "how loud is 45%" is hearing it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPalette
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
from .icons import close_icon
from .widgets import hint

# A trim is a multiplier on the master volume, so 100% means "as the file was recorded".
# The range runs past that because the master defaults below full scale, which leaves real
# headroom to lift a quiet import into line with the others.
MIN_GAIN_PERCENT = 0
MAX_GAIN_PERCENT = 200
DEFAULT_GAIN = 1.0

# Long enough that dragging a slider does not retrigger the sample on every pixel -- which
# is a stutter, not feedback -- and short enough to still feel like a response to the move.
REPLAY_DELAY_MS = 180


def _clamp_gain(gain: float) -> float:
    try:
        value = float(gain)
    except (TypeError, ValueError):
        return DEFAULT_GAIN
    return max(MIN_GAIN_PERCENT / 100.0, min(MAX_GAIN_PERCENT / 100.0, value))


def _to_percent(gain: float) -> int:
    return round(_clamp_gain(gain) * 100)


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


class _SoundRow(QFrame):
    """One imported sound: what it is, a way to hear it, its own level, and an X."""

    preview_requested = Signal(str)
    gain_changed = Signal()
    remove_requested = Signal(object)

    def __init__(self, sound: sounds.Sound, gain: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.sound = sound
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        label = QLabel(_describe(sound))
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(label, 1)

        play = QPushButton("Preview")
        play.setToolTip("Play this sound at the level set here.")
        play.clicked.connect(lambda: self.preview_requested.emit(self.sound.id))
        layout.addWidget(play)

        self.level = QSlider(Qt.Orientation.Horizontal)
        self.level.setRange(MIN_GAIN_PERCENT, MAX_GAIN_PERCENT)
        self.level.setValue(_to_percent(gain))
        self.level.setPageStep(10)
        # Sized from the font rather than a fixed pixel count: the text scale runs to 2x,
        # and a slider that does not grow with it ends up a thumb with nowhere to travel.
        self.level.setFixedWidth(max(80, round(self.fontMetrics().height() * 5)))
        self.level.setToolTip(
            f"How loud \u201c{sound.name}\u201d is, relative to the chime volume above. "
            "Remembered for this sound only."
        )
        self.level.setAccessibleName(f"Level for {sound.name}")
        layout.addWidget(self.level)

        self._readout = QLabel()
        # Reserve the width of the widest value up front, or every row's X shifts sideways
        # as its own number changes.
        self._readout.setMinimumWidth(self.fontMetrics().horizontalAdvance(" 200% "))
        self._readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._readout)
        self._show_percent(self.level.value())

        # The replay is debounced; the saved value is not. Autosave has a debounce of its
        # own, and a level that only persisted once the sound finished replaying would be
        # lost by closing the window promptly after a drag.
        self._replay = QTimer(self)
        self._replay.setSingleShot(True)
        self._replay.setInterval(REPLAY_DELAY_MS)
        self._replay.timeout.connect(lambda: self.preview_requested.emit(self.sound.id))
        self.level.valueChanged.connect(self._on_level_changed)

        self.remove = QPushButton()
        # Sized from the font for the same reason the slider is: the text scale runs to 2x,
        # and a button pinned at 30px next to 2x text reads as a stray dot.
        side = max(30, round(self.fontMetrics().height() * 1.5))
        self.remove.setFixedSize(QSize(side, side - 2))
        self.remove.setIconSize(QSize(round(side * 0.5), round(side * 0.5)))
        self.remove.setAccessibleName(f"Remove {sound.name}")
        self.remove.setToolTip(f"Remove \u201c{sound.name}\u201d from yada")
        self.remove.clicked.connect(lambda: self.remove_requested.emit(self.sound))
        layout.addWidget(self.remove)
        self.restyle()

    def _on_level_changed(self, value: int) -> None:
        self._show_percent(value)
        self.gain_changed.emit()
        self._replay.start()

    def _show_percent(self, value: int) -> None:
        self._readout.setText(f"{value}%")

    def gain(self) -> float:
        return self.level.value() / 100.0

    def set_gain(self, gain: float) -> None:
        """Show a stored level. Silent: adopting a saved value is not an edit, so it must
        neither replay the sound nor queue a save."""
        value = _to_percent(gain)
        if value == self.level.value():
            return
        self.level.blockSignals(True)
        self.level.setValue(value)
        self.level.blockSignals(False)
        self._show_percent(value)

    def restyle(self) -> None:
        """Redraw the X in the current palette's button text colour.

        Called on every palette change, because the icon is a pixmap: unlike the widgets
        Qt draws itself, it does not follow a theme switch without being asked.
        """
        self.remove.setIcon(close_icon(self.palette().color(QPalette.ColorRole.ButtonText)))

    def changeEvent(self, event) -> None:  # Qt naming convention
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ThemeChange):
            self.restyle()
        super().changeEvent(event)


class SoundLibraryEditor(QWidget):
    """Your imported sounds, each with its own level and an X, plus an import button."""

    library_changed = Signal()
    # Distinct from library_changed: a level is an ordinary edit for autosave to pick up,
    # where the library changing also has to rebuild the two pickers above.
    changed = Signal()
    preview_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Keyed by library id and kept for sounds that are not currently on screen, so a
        # level survives the rebuild that follows every import and removal.
        self._gains: dict[str, float] = {}
        self._rows = QVBoxLayout()
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(2)

        self._empty = hint(
            "No sounds of your own yet. Import a WAV, MP3, OGG, FLAC or M4A and it will "
            "appear in both pickers above."
        )
        self._levels_hint = hint(
            "Each sound keeps its own level, as a proportion of the chime volume above — "
            "so a quiet recording and a loud one can be made to land the same. Moving one "
            "plays it back at that level."
        )

        self.import_button = QPushButton("Import sound…")
        self.import_button.clicked.connect(self._on_import)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._empty)
        layout.addLayout(self._rows)
        layout.addWidget(self._levels_hint)
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
        self._levels_hint.setVisible(bool(custom))
        for sound in custom:
            self._rows.addWidget(self._build_row(sound))

    def _build_row(self, sound: sounds.Sound) -> QWidget:
        row = _SoundRow(sound, self._gains.get(sound.id, DEFAULT_GAIN))
        row.preview_requested.connect(self.preview_requested.emit)
        row.remove_requested.connect(self._on_remove)
        row.gain_changed.connect(self._on_gain_changed)
        return row

    def _visible_rows(self) -> list[_SoundRow]:
        return [
            widget
            for index in range(self._rows.count())
            if isinstance(widget := self._rows.itemAt(index).widget(), _SoundRow)
        ]

    def _on_gain_changed(self) -> None:
        self._gains = self.gains()
        self.changed.emit()

    # -- levels -------------------------------------------------------------------------

    def gains(self) -> dict[str, float]:
        """Every level worth storing, keyed by library id.

        Untouched sounds are left out rather than written as 1.0: the directory is the
        source of truth for what exists, so a settings file listing sounds it does not know
        about would be a second, staler answer to the same question. Levels for imports that
        have since been deleted drop out here for the same reason.
        """
        live = {row.sound.id: row.gain() for row in self._visible_rows()}
        return {
            sound_id: gain for sound_id, gain in live.items() if abs(gain - DEFAULT_GAIN) > 1e-6
        }

    def set_gains(self, gains: dict[str, float]) -> None:
        """Adopt stored levels and show them. Safe to call before any row exists."""
        self._gains = {
            str(sound_id): _clamp_gain(gain) for sound_id, gain in dict(gains or {}).items()
        }
        for row in self._visible_rows():
            row.set_gain(self._gains.get(row.sound.id, DEFAULT_GAIN))

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
    """The master chime volume, with the same audition-on-change behaviour as a sound row."""

    changed = Signal()
    preview_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(60)
        self.slider.valueChanged.connect(lambda _: self.changed.emit())
        self._readout = QLabel("60%")
        self._readout.setMinimumWidth(44)
        self.slider.valueChanged.connect(lambda v: self._readout.setText(f"{v}%"))

        # Dragging this fired a fresh play on every pixel of travel, and QSoundEffect
        # restarts rather than overlaps, so the sample never got past its first few
        # milliseconds -- a rattle instead of the chime being judged.
        self._replay = QTimer(self)
        self._replay.setSingleShot(True)
        self._replay.setInterval(REPLAY_DELAY_MS)
        self._replay.timeout.connect(self.preview_requested.emit)
        self.slider.valueChanged.connect(lambda _: self._replay.start())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Volume"))
        layout.addWidget(self.slider, 1)
        layout.addWidget(self._readout)

    def value(self) -> float:
        return self.slider.value() / 100.0

    def set_value(self, volume: float) -> None:
        self.slider.setValue(round(max(0.0, min(1.0, volume)) * 100))
        # Loading settings is not an edit, so nothing should play because of it.
        self._replay.stop()
