"""The Recordings tab: play back what you said, and transcribe it again.

Transcription happens after you stop talking, so a network error at that moment used to
throw away a complete recording. yada keeps the last few now, and this is where you can
hear them and try again.

Playback is `QAudioSink` fed from a `QBuffer`, not `QMediaPlayer`. These are yada's own
24 kHz mono PCM16 files, so there is no format to detect -- and QMediaPlayer needs a
platform media backend plugin that a frozen build may or may not have shipped, whereas
writing bytes to an audio device does not. Seeking is then a byte offset rather than a
backend feature.
"""

from __future__ import annotations

import contextlib
import wave
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..pipeline.recordings import Recording
from .widgets import hint

# Position updates often enough for the scrubber to look continuous, rarely enough to
# cost nothing.
TICK_MS = 60


class WavPlayer(QObject):
    """Plays one PCM16 WAV, with a position and the ability to seek."""

    position_changed = Signal(float)  # seconds
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sink = None
        self._buffer: QBuffer | None = None
        self._bytes_per_second = 1
        self.duration = 0.0
        self.error: str | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    # -- loading ------------------------------------------------------------------------

    def load(self, path: Path) -> bool:
        self.stop()
        self.error = None
        try:
            with contextlib.closing(wave.open(str(path), "rb")) as handle:
                channels = handle.getnchannels()
                width = handle.getsampwidth()
                rate = handle.getframerate()
                frames = handle.readframes(handle.getnframes())
        except Exception as exc:  # noqa: BLE001 - a bad file must not take the tab down
            self.error = f"could not read the recording ({type(exc).__name__})"
            return False

        try:
            from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

            fmt = QAudioFormat()
            fmt.setSampleRate(rate)
            fmt.setChannelCount(channels)
            fmt.setSampleFormat(
                QAudioFormat.SampleFormat.Int16 if width == 2 else QAudioFormat.SampleFormat.UInt8
            )
            device = QMediaDevices.defaultAudioOutput()
            if device is None:
                self.error = "no audio output device"
                return False
            self._sink = QAudioSink(device, fmt, self)
            self._sink.stateChanged.connect(self._on_state)
        except Exception as exc:  # noqa: BLE001
            self.error = f"audio playback unavailable ({type(exc).__name__})"
            return False

        self._bytes_per_second = max(1, rate * channels * width)
        self.duration = len(frames) / self._bytes_per_second
        self._buffer = QBuffer(self)
        self._buffer.setData(QByteArray(frames))
        self._buffer.open(QBuffer.OpenModeFlag.ReadOnly)
        return True

    # -- transport ----------------------------------------------------------------------

    @property
    def playing(self) -> bool:
        if self._sink is None:
            return False
        from PySide6.QtMultimedia import QAudio

        return self._sink.state() == QAudio.State.ActiveState

    def play(self) -> None:
        if self._sink is None or self._buffer is None:
            return
        from PySide6.QtMultimedia import QAudio

        if self._sink.state() == QAudio.State.SuspendedState:
            self._sink.resume()
        else:
            if self._buffer.atEnd():
                self._buffer.seek(0)
            self._sink.start(self._buffer)
        self._timer.start()

    def pause(self) -> None:
        if self._sink is not None:
            self._sink.suspend()
        self._timer.stop()

    def stop(self) -> None:
        self._timer.stop()
        if self._sink is not None:
            with contextlib.suppress(Exception):
                self._sink.stop()
        if self._buffer is not None:
            self._buffer.seek(0)
        self.position_changed.emit(0.0)

    def seek(self, seconds: float) -> None:
        """Move to a point in the recording, aligned to a whole frame."""
        if self._buffer is None:
            return
        was_playing = self.playing
        offset = int(max(0.0, seconds) * self._bytes_per_second)
        # Landing mid-sample would shift every following byte and turn the audio to noise.
        offset -= offset % 2
        offset = min(offset, self._buffer.size())
        if was_playing:
            self._sink.stop()
        self._buffer.seek(offset)
        if was_playing:
            self._sink.start(self._buffer)
        self.position_changed.emit(offset / self._bytes_per_second)

    # -- internals ----------------------------------------------------------------------

    def _tick(self) -> None:
        if self._buffer is None:
            return
        self.position_changed.emit(self._buffer.pos() / self._bytes_per_second)

    def _on_state(self, state) -> None:
        from PySide6.QtMultimedia import QAudio

        if state in (QAudio.State.IdleState, QAudio.State.StoppedState):
            self._timer.stop()
            if self._buffer is not None and self._buffer.atEnd():
                self.position_changed.emit(self.duration)
                self.finished.emit()


class RecordingRow(QWidget):
    """One recording: what it says, a play button, a scrubber, and another attempt."""

    transcribe_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, recording: Recording, audio: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.recording = recording
        self._audio = audio
        self._player = WavPlayer(self)
        self._loaded = False
        self._scrubbing = False

        self.summary = QLabel(f"{recording.when} · {recording.duration_seconds:.1f}s")
        summary_font = self.summary.font()
        summary_font.setBold(True)
        self.summary.setFont(summary_font)
        self.detail = hint(recording.summary())
        self.detail.setWordWrap(True)

        self.play_button = QPushButton("Play")
        self.play_button.setFixedWidth(self.play_button.sizeHint().width())
        self.play_button.clicked.connect(self._toggle_play)

        self.scrubber = QSlider(Qt.Orientation.Horizontal)
        self.scrubber.setRange(0, max(1, int(recording.duration_seconds * 100)))
        self.scrubber.sliderPressed.connect(self._begin_scrub)
        self.scrubber.sliderReleased.connect(self._end_scrub)

        self.elapsed = QLabel("0.0s")
        self.elapsed.setMinimumWidth(self.elapsed.fontMetrics().horizontalAdvance("00.0s"))

        self.transcribe_button = QPushButton("Transcribe again")
        self.transcribe_button.clicked.connect(
            lambda: self.transcribe_requested.emit(self.recording.id)
        )
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(lambda: self.delete_requested.emit(self.recording.id))

        transport = QHBoxLayout()
        transport.addWidget(self.play_button)
        transport.addWidget(self.scrubber, 1)
        transport.addWidget(self.elapsed)
        transport.addWidget(self.transcribe_button)
        transport.addWidget(self.delete_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.summary)
        layout.addWidget(self.detail)
        layout.addLayout(transport)

        self._player.position_changed.connect(self._on_position)
        self._player.finished.connect(self._on_finished)

    # -- playback -----------------------------------------------------------------------

    def _ensure_loaded(self) -> bool:
        """Loaded on first play, not on building the tab.

        Five recordings is five audio devices and five copies of the audio in memory, for
        a tab the user may only be visiting to read the text.
        """
        if self._loaded:
            return True
        self._loaded = self._player.load(self._audio)
        if not self._loaded:
            self.detail.setText(self._player.error or "could not play this recording")
            self.play_button.setEnabled(False)
        return self._loaded

    def _toggle_play(self) -> None:
        if not self._ensure_loaded():
            return
        if self._player.playing:
            self._player.pause()
            self.play_button.setText("Play")
        else:
            self._player.play()
            self.play_button.setText("Pause")

    def _begin_scrub(self) -> None:
        self._scrubbing = True

    def _end_scrub(self) -> None:
        self._scrubbing = False
        if self._ensure_loaded():
            self._player.seek(self.scrubber.value() / 100.0)

    def _on_position(self, seconds: float) -> None:
        self.elapsed.setText(f"{seconds:.1f}s")
        if not self._scrubbing:
            self.scrubber.blockSignals(True)
            self.scrubber.setValue(int(seconds * 100))
            self.scrubber.blockSignals(False)

    def _on_finished(self) -> None:
        self.play_button.setText("Play")

    def stop(self) -> None:
        self._player.stop()
        self.play_button.setText("Play")


class RecordingsPane(QWidget):
    """The tab itself: the rows, and the controls that apply to all of them."""

    transcribe_requested = Signal(str)
    delete_requested = Signal(str)
    clear_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[RecordingRow] = []

        self.status = hint("")
        self._list_box = QGroupBox("Recent recordings")
        self._list_layout = QVBoxLayout(self._list_box)
        # Rows are three lines each; without a gap they read as one wall of text.
        self._list_layout.setSpacing(max(12, round(self.fontMetrics().height() * 0.8)))
        self.empty_label = hint(
            "Nothing recorded yet. Recordings appear here after your first dictation."
        )
        self._list_layout.addWidget(self.empty_label)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_requested.emit)
        self.clear_button = QPushButton("Delete all recordings")
        self.clear_button.clicked.connect(self._confirm_clear)
        buttons = QHBoxLayout()
        buttons.addWidget(refresh)
        buttons.addStretch(1)
        buttons.addWidget(self.clear_button)
        button_holder = QWidget()
        button_holder.setLayout(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(
            hint(
                "yada keeps your most recent recordings so that a transcription which "
                "fails — a dropped connection, usually — can be tried again instead of "
                "said again. Hold the dictation shortcut for three seconds to retry the "
                "newest one without opening this window.\n\n"
                "These are audio files of what you dictated, kept on this machine only. "
                "How many to keep is on the System tab, and 0 keeps none."
            )
        )
        layout.addWidget(self.status)
        layout.addWidget(self._list_box)
        layout.addWidget(button_holder)
        layout.addStretch(1)

    def set_recordings(self, recordings: list[Recording], audio_for) -> None:
        """Rebuild the rows. `audio_for` maps a recording id to its file."""
        for row in self._rows:
            row.stop()
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        self.empty_label.setVisible(not recordings)
        for recording in recordings:
            row = RecordingRow(recording, audio_for(recording.id), self)
            row.transcribe_requested.connect(self.transcribe_requested.emit)
            row.delete_requested.connect(self.delete_requested.emit)
            self._list_layout.addWidget(row)
            self._rows.append(row)
        self.clear_button.setEnabled(bool(recordings))

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def stop_playback(self) -> None:
        """Called when the window hides: nothing should keep playing behind it."""
        for row in self._rows:
            row.stop()

    def _confirm_clear(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete all recordings?")
        box.setText("Delete every stored recording?")
        box.setInformativeText(
            "The audio files are removed from this machine. Anything not yet transcribed "
            "cannot be retried afterwards."
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Discard
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Discard:
            self.clear_requested.emit()
