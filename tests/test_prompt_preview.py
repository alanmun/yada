"""The Transform tab's preview must be exactly what gets sent.

Its whole value is that it cannot be wrong, so it is built by the same functions the
pipeline calls -- `build_system_prompt` and `build_user_prompt` -- rather than by a second
implementation that could drift.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from yada.config import Settings, TransformStep, Vocabulary
from yada.pipeline.transform import build_system_prompt, build_user_prompt
from yada.ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    import yada.config as cfg
    import yada.output.sounds as snd

    monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(snd, "config_dir", lambda: tmp_path)
    settings = Settings()
    settings.transform.enabled = True
    settings.transform.steps = [
        TransformStep(type="prompt_transform", system_prompt="Tidy this up.")
    ]
    settings.vocabulary.terms = ["yada", "PortAudio"]
    w = SettingsWindow(settings)
    yield w
    w.close()


def test_the_preview_matches_what_the_pipeline_would_build(window):
    """The assertion that gives the feature its point: identical to the real prompt."""
    step = TransformStep(type="prompt_transform", system_prompt="Tidy this up.")
    vocab = Vocabulary(terms=["yada", "PortAudio"], context_prompt="", languages=["en"])
    text = window.prompt_preview.toPlainText()

    assert build_system_prompt(step, vocab) in text
    assert build_user_prompt(step, window.PREVIEW_SPEECH) in text


def test_the_placeholder_stands_in_for_speech(window):
    text = window.prompt_preview.toPlainText()
    assert "just a preview" in text
    assert "{{input}}" not in text, "the template must be filled in, not shown raw"


def test_the_vocabulary_appears_because_it_is_actually_sent(window):
    """The appended vocabulary block is the least obvious part of the real prompt."""
    text = window.prompt_preview.toPlainText()
    assert "yada" in text
    assert "PortAudio" in text


def test_it_updates_as_the_prompt_is_edited(window, qapp):
    """No preview button to press: it follows the edit."""
    before = window.prompt_preview.toPlainText()
    window.steps.set_steps(
        [TransformStep(type="prompt_transform", system_prompt="Something else entirely.")]
    )
    window._schedule_save()
    qapp.processEvents()

    after = window.prompt_preview.toPlainText()
    assert after != before
    assert "Something else entirely." in after


def test_it_updates_when_the_vocabulary_changes(window, qapp):
    """The terms live on another tab but end up in this prompt."""
    window.vocab_terms.set_values(["Kubernetes"])
    window._schedule_save()
    qapp.processEvents()
    assert "Kubernetes" in window.prompt_preview.toPlainText()


def test_transforms_off_says_nothing_is_sent(window, qapp):
    """Rather than showing a prompt that would not be used."""
    window.tf_enabled.setChecked(False)
    window._schedule_save()
    qapp.processEvents()
    text = window.prompt_preview.toPlainText()
    assert "Transforms are off" in text
    assert "SYSTEM:" not in text


def test_a_disabled_step_is_left_out(window, qapp):
    window.steps.set_steps(
        [
            TransformStep(type="prompt_transform", system_prompt="Included.", enabled=True),
            TransformStep(type="prompt_transform", system_prompt="Excluded.", enabled=False),
        ]
    )
    window._schedule_save()
    qapp.processEvents()
    text = window.prompt_preview.toPlainText()
    assert "Included." in text
    assert "Excluded." not in text


def test_a_find_replace_step_says_it_sends_nothing(window, qapp):
    """It runs locally, so claiming it is 'sent' would be wrong."""
    window.steps.set_steps(
        [TransformStep(type="find_replace", find="teh", replace="the", enabled=True)]
    )
    window._schedule_save()
    qapp.processEvents()
    text = window.prompt_preview.toPlainText()
    assert "nothing is sent to a model" in text
    assert "teh" in text and "the" in text


def test_a_later_step_is_shown_receiving_the_earlier_output(window, qapp):
    """It cannot know the text, so it must not pretend to."""
    window.steps.set_steps(
        [
            TransformStep(type="prompt_transform", system_prompt="First.", enabled=True),
            TransformStep(type="prompt_transform", system_prompt="Second.", enabled=True),
        ]
    )
    window._schedule_save()
    qapp.processEvents()
    text = window.prompt_preview.toPlainText()
    assert "Step 1" in text and "Step 2" in text
    assert "whatever step 1 returns" in text


def test_no_enabled_steps_says_so(window, qapp):
    window.steps.set_steps([])
    window._schedule_save()
    qapp.processEvents()
    assert "No enabled steps" in window.prompt_preview.toPlainText()


def test_the_preview_is_read_only(window):
    assert window.prompt_preview.isReadOnly()
