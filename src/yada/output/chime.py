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


def effective_volume(master: float, remainder: float) -> float:
    """The playback volume for a file, once its boost has been baked into the samples.

    `remainder` is what sounds.playable() could not bake: attenuation, which always fits,
    plus any boost beyond the file's own headroom, which does not and is clamped here. A
    remainder that is not a number at all -- settings.json is documented as hand-editable --
    reads as "untouched" rather than as silence, because a chime that never fires is the
    harder failure to diagnose.
    """
    try:
        trim = float(remainder)
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
        # What each stage points at, as opposed to _for_stage's file actually played: a
        # level change re-renders the latter, so the sound it came from has to be kept.
        self._stage_sounds: dict[Stage, sounds.Sound] = {}
        # Per played file, whatever part of its level could not be baked into the samples.
        self._remainders: dict[Path, float] = {}
        # Previewed sounds are kept loaded so repeated Preview clicks stay instant, and so
        # _prune does not evict something the user is auditioning.
        self._previewed: set[Path] = set()
        # QSoundEffect loads a newly assigned source asynchronously. Calling play() while
        # it is still Loading is not a promise to start at Ready: on Windows it can consume
        # the request as silence or begin partway through the sample. Remember the newest
        # request and fire it exactly once when its effect reports that it is loaded.
        self._pending_play: Path | None = None
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
            self._stage_sounds[stage] = sound

        self._resolve_stages()

    def preload(self) -> None:
        """Load the defaults. Kept for callers that have no settings yet."""
        self.configure(
            listening=sounds.DEFAULT_FOR_STAGE[Stage.LISTENING],
            transcription=sounds.DEFAULT_FOR_STAGE[Stage.TRANSCRIPTION],
            transformation=sounds.DEFAULT_FOR_STAGE[Stage.TRANSFORMATION],
        )

    def _resolve_stages(self) -> None:
        """Work out which file each stage actually plays, and have it loaded and ready.

        Done here rather than at play time because a boost is rendered to disk on first use,
        and a chime is the one sound in the app that must not wait for anything.
        """
        for stage, sound in self._stage_sounds.items():
            self._for_stage[stage] = self._prepare(sound)
        self._prune()
        self._apply_volume()

    def _prepare(self, sound: sounds.Sound) -> Path:
        """Render this sound at its level if need be, load it, and return what to play."""
        path, remainder = sounds.playable(sound, self._gains.get(sound.id, 1.0))
        self._remainders[path] = remainder
        self._load(path)
        return path

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
        effect.setVolume(self._level(path))
        # Connect before setSource(): a small local WAV may reach Ready synchronously, and
        # missing that transition would leave a preview waiting forever. The callback
        # queries the effect instead of trusting a signal argument; statusChanged has no
        # payload in Qt, and the star keeps this tolerant of binding differences.
        effect.statusChanged.connect(
            lambda *_, loaded_path=path: self._on_effect_status(loaded_path)
        )
        self._effects[path] = effect
        effect.setSource(QUrl.fromLocalFile(str(path)))

    def _level(self, path: Path) -> float:
        return effective_volume(self._volume, self._remainders.get(path, 1.0))

    def _prune(self) -> None:
        """Drop effects nothing points at any more, so swapping sounds does not leak."""
        in_use = set(self._for_stage.values())
        for path in [p for p in self._effects if p not in in_use and p not in self._previewed]:
            self._effects.pop(path, None)
            self._remainders.pop(path, None)
            if self._pending_play == path:
                self._pending_play = None

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
        if dict(gains) == self._gains:
            return
        self._gains = dict(gains)
        # A level change can move a stage onto a different file entirely -- a rendered copy
        # instead of the original, or the other way round -- so the stages are re-resolved
        # rather than merely re-volumed.
        self._resolve_stages()

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
        path = self._prepare(sound)
        self._previewed.add(path)
        self._apply_volume()
        self._play_path(path)

    def _play_path(self, path: Path) -> None:
        effect = self._effects.get(path)
        if effect is None:
            return
        # The latest request supersedes an older sound that is still loading. Otherwise a
        # slow first preview can arrive after the user has already selected another sound.
        self._pending_play = path
        self._on_effect_status(path)

    def _on_effect_status(self, path: Path) -> None:
        """Play a requested effect only after Qt has fully loaded its decoded buffer."""
        effect = self._effects.get(path)
        if effect is None:
            if self._pending_play == path:
                self._pending_play = None
            return
        try:
            if effect.isLoaded():  # type: ignore[attr-defined]
                if self._pending_play != path:
                    return
                # Clear first because play() itself may synchronously emit a status signal
                # on some backends. A second callback must not start the sample again.
                self._pending_play = None
                effect.play()  # type: ignore[attr-defined]
                return

            status = effect.status()  # type: ignore[attr-defined]
            status_name = getattr(status, "name", str(status).rsplit(".", 1)[-1])
            if status_name == "Error":
                if self._pending_play == path:
                    self._pending_play = None
                self.last_error = f"could not load sound: {path.name}"
        except Exception as exc:  # noqa: BLE001
            if self._pending_play == path:
                self._pending_play = None
            self.last_error = str(exc)
