"""The two notification sounds.

One chime when your words are ready, a second when the cleanup pass finishes. The defaults
differ in contour -- the first rises, the second falls and settles -- so they are
distinguishable without paying attention, which is the entire point of an audio cue. Either
can be swapped for your own sound; see output/sounds.py for how the library works.

QSoundEffect rather than QMediaPlayer: it keeps short samples decoded in memory and plays
with minimal latency, whereas QMediaPlayer spins up a pipeline per play and can lag by a
noticeable, variable fraction of a second -- on the one cue whose job is to fire the
instant something finishes.
"""

from __future__ import annotations

from pathlib import Path

from ..pipeline.session import Stage
from . import sounds


class ChimePlayer:
    """Holds preloaded effects, keyed by file path.

    Loading on first play would put an audible delay on the very sound that signals
    completion, so effects are built up front and cached. The cache is keyed by path rather
    than by stage, so using one sound for both stages loads it once.
    """

    def __init__(self, volume: float = 0.6) -> None:
        self._effects: dict[Path, object] = {}
        self._for_stage: dict[Stage, Path] = {}
        self._volume = max(0.0, min(1.0, volume))
        # Previewed sounds are kept loaded so repeated Preview clicks stay instant, and so
        # _prune does not evict something the user is auditioning.
        self._previewed: set[Path] = set()
        self.last_error: str | None = None

    # -- setup --------------------------------------------------------------------------

    def configure(
        self,
        *,
        listening: str | None = None,
        transcription: str | None = None,
        transformation: str | None = None,
        volume: float | None = None,
    ) -> None:
        """Point each stage at a library id and preload it.

        A selection that no longer resolves -- an imported sound the user deleted, or a
        config copied from another machine -- silently falls back to that stage's built-in.
        Silence would be a far more confusing failure than the wrong chime.
        """
        if volume is not None:
            self._volume = max(0.0, min(1.0, volume))

        wanted = {
            Stage.LISTENING: listening,
            Stage.TRANSCRIPTION: transcription,
            Stage.TRANSFORMATION: transformation,
        }
        for stage, sound_id in wanted.items():
            if sound_id is None:
                continue
            sound = sounds.resolve_or_default(sound_id, stage)
            if sound is None:
                self.last_error = f"no sound available for {stage}"
                self._for_stage.pop(stage, None)
                continue
            self._for_stage[stage] = sound.path
            self._load(sound.path)

        self._prune()
        self._apply_volume()

    def preload(self) -> None:
        """Load the defaults. Kept for callers that have no settings yet."""
        self.configure(
            listening=sounds.DEFAULT_FOR_STAGE[Stage.LISTENING],
            transcription=sounds.DEFAULT_FOR_STAGE[Stage.TRANSCRIPTION],
            transformation=sounds.DEFAULT_FOR_STAGE[Stage.TRANSFORMATION],
        )

    def _load(self, path: Path) -> None:
        if path in self._effects:
            return
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect
        except ImportError as exc:
            self.last_error = f"audio playback unavailable ({exc})"
            return
        if not path.exists():
            self.last_error = f"missing sound file: {path.name}"
            return
        effect = QSoundEffect()
        effect.setSource(QUrl.fromLocalFile(str(path)))
        effect.setVolume(self._volume)
        self._effects[path] = effect

    def _prune(self) -> None:
        """Drop effects nothing points at any more, so swapping sounds does not leak."""
        in_use = set(self._for_stage.values())
        for path in [p for p in self._effects if p not in in_use and p not in self._previewed]:
            self._effects.pop(path, None)

    def _apply_volume(self) -> None:
        for effect in self._effects.values():
            try:
                effect.setVolume(self._volume)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        self._apply_volume()

    @property
    def volume(self) -> float:
        return self._volume

    # -- playback -----------------------------------------------------------------------

    def play(self, stage: Stage) -> None:
        """Fire and forget. A failed chime must never interrupt the dictation flow."""
        path = self._for_stage.get(stage)
        if path is None:
            return
        self._play_path(path)

    def preview(self, sound_id: str) -> None:
        """Play any library sound, for the Preview button in settings."""
        sound = sounds.resolve(sound_id)
        if sound is None:
            return
        self._previewed.add(sound.path)
        self._load(sound.path)
        self._apply_volume()
        self._play_path(sound.path)

    def _play_path(self, path: Path) -> None:
        effect = self._effects.get(path)
        if effect is None:
            return
        try:
            effect.play()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
