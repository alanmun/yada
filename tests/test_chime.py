"""Playback-state tests that do not need a real speaker or audio backend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yada.output.chime import ChimePlayer


@dataclass
class FakeStatus:
    name: str


class FakeEffect:
    def __init__(self, *, loaded: bool = False, status: str = "Loading") -> None:
        self.loaded = loaded
        self.current_status = FakeStatus(status)
        self.plays = 0

    def isLoaded(self) -> bool:  # Qt naming convention
        return self.loaded

    def status(self) -> FakeStatus:
        return self.current_status

    def play(self) -> None:
        self.plays += 1


def _player_with(path: Path, effect: FakeEffect) -> ChimePlayer:
    player = ChimePlayer()
    player._effects[path] = effect
    return player


def test_a_loading_effect_waits_until_ready_before_playing(tmp_path) -> None:
    path = tmp_path / "preview.wav"
    effect = FakeEffect()
    player = _player_with(path, effect)

    player._play_path(path)
    assert effect.plays == 0
    assert player._pending_play == path

    effect.loaded = True
    effect.current_status = FakeStatus("Ready")
    player._on_effect_status(path)

    assert effect.plays == 1
    assert player._pending_play is None


def test_ready_notifications_do_not_replay_a_consumed_request(tmp_path) -> None:
    path = tmp_path / "preview.wav"
    effect = FakeEffect()
    player = _player_with(path, effect)
    player._play_path(path)

    effect.loaded = True
    effect.current_status = FakeStatus("Ready")
    player._on_effect_status(path)
    player._on_effect_status(path)

    assert effect.plays == 1


def test_the_newest_preview_supersedes_a_sound_that_is_still_loading(tmp_path) -> None:
    old_path = tmp_path / "old.wav"
    new_path = tmp_path / "new.wav"
    old = FakeEffect()
    new = FakeEffect()
    player = ChimePlayer()
    player._effects.update({old_path: old, new_path: new})

    player._play_path(old_path)
    player._play_path(new_path)

    old.loaded = True
    old.current_status = FakeStatus("Ready")
    player._on_effect_status(old_path)
    assert old.plays == 0, "a late sound must not surprise the user"

    new.loaded = True
    new.current_status = FakeStatus("Ready")
    player._on_effect_status(new_path)
    assert new.plays == 1


def test_a_load_error_consumes_the_request_and_records_a_reason(tmp_path) -> None:
    path = tmp_path / "broken.wav"
    effect = FakeEffect(status="Error")
    player = _player_with(path, effect)

    player._play_path(path)

    assert effect.plays == 0
    assert player._pending_play is None
    assert player.last_error == "could not load sound: broken.wav"


def test_an_already_loaded_effect_plays_immediately(tmp_path) -> None:
    path = tmp_path / "cached.wav"
    effect = FakeEffect(loaded=True, status="Ready")
    player = _player_with(path, effect)

    player._play_path(path)

    assert effect.plays == 1
    assert player._pending_play is None
