"""The two notification sounds.

One chime when your words are ready, a second when the cleanup pass finishes. They differ in
contour -- the first rises, the second falls and settles -- so they are distinguishable
without paying attention, which is the entire point of an audio cue.

QSoundEffect rather than QMediaPlayer: it keeps short samples decoded in memory and plays
with low latency, whereas QMediaPlayer spins up a pipeline and can lag by a noticeable
fraction of a second.
"""

from __future__ import annotations

from pathlib import Path

from ..pipeline.session import Stage

SOUNDS_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"

FILES: dict[Stage, str] = {
    Stage.TRANSCRIPTION: "transcription.wav",
    Stage.TRANSFORMATION: "transformation.wav",
}


class ChimePlayer:
    """Holds preloaded effects. Constructing these lazily on first play causes an audible
    delay on the very sound that is supposed to signal completion, so they are loaded up
    front and reused."""

    def __init__(self, volume: float = 0.6) -> None:
        self._effects: dict[Stage, object] = {}
        self._volume = volume
        self.last_error: str | None = None

    def preload(self) -> None:
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect
        except ImportError as exc:
            self.last_error = f"audio playback unavailable ({exc})"
            return
        for stage, filename in FILES.items():
            path = SOUNDS_DIR / filename
            if not path.exists():
                self.last_error = f"missing sound file: {path.name}"
                continue
            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(str(path)))
            effect.setVolume(self._volume)
            self._effects[stage] = effect

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        for effect in self._effects.values():
            effect.setVolume(self._volume)  # type: ignore[attr-defined]

    def play(self, stage: Stage) -> None:
        """Fire and forget. A failed chime must never interrupt the dictation flow."""
        effect = self._effects.get(stage)
        if effect is None:
            return
        try:
            effect.play()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
