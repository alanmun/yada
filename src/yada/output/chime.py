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


def effective_volume(master: float, gain: float) -> float:
    """How loud one file plays: the master volume scaled by that file's own trim.

    A trim above 1.0 is allowed and does what you would expect until the product reaches
    full scale, past which the file is already as loud as the device will play it. A trim
    that is not a number at all -- settings.json is documented as hand-editable -- is read
    as "untouched" rather than as silence, because a missing chime is the harder failure to
    diagnose.
    """
    try:
        trim = float(gain)
    except (TypeError, ValueError):
        trim = 1.0
    return max(0.0, min(1.0, max(0.0, min(1.0, master)) * max(0.0, trim)))


class ChimePlayer:
    """Holds preloaded effects, keyed by file path.

    Loading on first play would put an audible delay on the very sound that signals
    completion, so effects are built up front and cached. The cache is keyed by path rather
    than by stage, so using one sound for both stages loads it once.
    """

    def __init__(self, volume: float = 0.6, gains: dict[str, float] | None = None) -> None:
        self._effects: dict[Path, object] = {}
        self._for_stage: dict[Stage, Path] = {}
        self._volume = max(0.0, min(1.0, volume))
        # Per-sound trim, keyed by library id. The master volume sets how loud yada is; this
        # sets how loud one file is relative to the others, which is the only way two
        # imports mastered at different levels can be made to sit at the same loudness.
        self._gains: dict[str, float] = dict(gains or {})
        # Effects are cached by path, but the trim is keyed by id, so the mapping between
        # them has to be remembered at load time -- it is the only place both are in hand.
        self._ids: dict[Path, str] = {}
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
        gains: dict[str, float] | None = None,
    ) -> None:
        """Point each stage at a library id and preload it.

        A selection that no longer resolves -- an imported sound the user deleted, or a
        config copied from another machine -- silently falls back to that stage's built-in.
        Silence would be a far more confusing failure than the wrong chime.
        """
        if volume is not None:
            self._volume = max(0.0, min(1.0, volume))
        if gains is not None:
            self._gains = dict(gains)

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
            self._load(sound)

        self._prune()
        self._apply_volume()

    def preload(self) -> None:
        """Load the defaults. Kept for callers that have no settings yet."""
        self.configure(
            listening=sounds.DEFAULT_FOR_STAGE[Stage.LISTENING],
            transcription=sounds.DEFAULT_FOR_STAGE[Stage.TRANSCRIPTION],
            transformation=sounds.DEFAULT_FOR_STAGE[Stage.TRANSFORMATION],
        )

    def _load(self, sound: sounds.Sound) -> None:
        path = sound.path
        # Recorded even for an already-loaded effect: the same file can be reached by a
        # stage and by a preview, and losing the id would silently drop its trim.
        self._ids[path] = sound.id
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
        effect.setVolume(self._level(path))
        self._effects[path] = effect

    def _prune(self) -> None:
        """Drop effects nothing points at any more, so swapping sounds does not leak."""
        in_use = set(self._for_stage.values())
        for path in [p for p in self._effects if p not in in_use and p not in self._previewed]:
            self._effects.pop(path, None)
            self._ids.pop(path, None)

    def _level(self, path: Path) -> float:
        """The volume to play the effect cached at `path` at."""
        return effective_volume(self._volume, self._gains.get(self._ids.get(path, ""), 1.0))

    def _apply_volume(self) -> None:
        for path, effect in self._effects.items():
            try:
                effect.setVolume(self._level(path))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        self._apply_volume()

    def set_gains(self, gains: dict[str, float]) -> None:
        """Replace the per-sound trims and apply them immediately.

        Used by the settings window so auditioning a slider is heard at the level being
        dragged, not the one last written to disk a debounce ago.
        """
        self._gains = dict(gains)
        self._apply_volume()

    def gain(self, sound_id: str) -> float:
        return self._gains.get(sound_id, 1.0)

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
        self._load(sound)
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
