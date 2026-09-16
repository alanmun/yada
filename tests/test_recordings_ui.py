"""Playback controls in the Recordings tab."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QBuffer, QByteArray
from PySide6.QtMultimedia import QAudio
from PySide6.QtWidgets import QApplication

from yada.pipeline.recordings import Recording
from yada.ui.recordings_tab import RecordingRow, WavPlayer


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Sink:
    """Audio backend that stays idle after accepting playback data."""

    def __init__(self) -> None:
        self.suspended = False

    def state(self):
        return QAudio.State.IdleState

    def start(self, _buffer) -> None:
        pass

    def suspend(self) -> None:
        self.suspended = True


def test_pause_uses_the_selected_transport_state_even_if_the_backend_is_idle(app):
    player = WavPlayer()
    sink = _Sink()
    buffer = QBuffer()
    buffer.setData(QByteArray(b"audio"))
    buffer.open(QBuffer.OpenModeFlag.ReadOnly)
    player._sink = sink
    player._buffer = buffer

    player.play()
    assert player.playing is True

    player.pause()
    assert sink.suspended is True
    assert player.playing is False


def test_play_button_is_wide_enough_for_pause(app, tmp_path):
    recording = Recording(
        id="recording",
        recorded_at=datetime.now(UTC).isoformat(),
        duration_seconds=1.0,
    )
    row = RecordingRow(recording, tmp_path / "recording.wav")

    row.play_button.setText("Pause")
    assert row.play_button.width() >= row.play_button.sizeHint().width()
