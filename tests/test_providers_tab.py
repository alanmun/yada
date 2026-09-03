"""One provider on screen at a time, and a curated pick for each.

Every provider used to be stacked on the page at once, so setting up OpenAI meant scrolling
past OpenRouter's key field and notes.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from yada.config import Settings
from yada.providers.base import Modality
from yada.providers.registry import SPECS
from yada.ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp):
    w = SettingsWindow(Settings())
    w.focus_tab("Providers")
    w.show()
    qapp.processEvents()
    yield w
    w.close()


def test_only_the_chosen_provider_is_on_screen(window, qapp):
    assert window._provider_pages.count() == len(SPECS)
    for index, provider_id in enumerate(SPECS):
        window.provider_chooser.setCurrentIndex(index)
        qapp.processEvents()
        assert window.provider_chooser.currentData() == provider_id
        assert window._provider_pages.currentIndex() == index
        for other in range(window._provider_pages.count()):
            page = window._provider_pages.widget(other)
            assert page.isVisibleTo(window._provider_pages) is (other == index), (
                f"page {other} visible while {index} is chosen"
            )


def test_every_provider_still_has_a_key_field(window):
    """Hiding a provider must not stop you configuring it -- or reading its stored key."""
    assert set(window._key_fields) == set(SPECS)


def test_choosing_a_provider_does_not_save_settings(window, qapp):
    """The chooser is navigation. Saving on it would write a file for a click.

    It also must not look like an edit: the settings file is rewritten on every save, and
    a spurious one during startup is how other values have been clobbered before.
    """
    saved: list = []
    window.saved.connect(saved.append)
    window.provider_chooser.setCurrentIndex(1)
    qapp.processEvents()
    window.flush_pending_save()
    qapp.processEvents()
    assert saved == []


def test_each_provider_advertises_a_recommendation(window, qapp):
    """The user asked for a curated pick per provider; assert every provider has one."""
    for provider_id, spec in SPECS.items():
        if spec.transcribes:
            assert spec.recommended_transcription, f"{provider_id} has no transcription pick"
        if spec.transforms:
            assert spec.recommended_transform, f"{provider_id} has no transform pick"


def test_a_recommendation_falls_through_to_what_is_actually_offered():
    """A retired first choice must not recommend something nobody can select."""
    spec = SPECS["openai"]
    assert spec.recommended(Modality.TRANSCRIPTION, ["gpt-live-transcribe"]) == (
        "gpt-live-transcribe"
    )
    # First choice gone: fall through rather than recommending it anyway.
    assert spec.recommended(Modality.TRANSCRIPTION, ["gpt-transcribe"]) == "gpt-transcribe"
    # Nothing curated is available: say nothing rather than invent a pick.
    assert spec.recommended(Modality.TRANSCRIPTION, ["something-else"]) == ""


def test_openrouter_mirrors_the_openai_picks():
    """OpenRouter is a router: its OpenAI models are the same models, prefixed.

    Derived rather than hand-listed, so changing the OpenAI pick cannot leave OpenRouter
    recommending last month's model.
    """
    openai, openrouter = SPECS["openai"], SPECS["openrouter"]

    for modality, ours, theirs in (
        (
            Modality.TRANSCRIPTION,
            openai.recommended_transcription,
            openrouter.recommended_transcription,
        ),
        (Modality.TEXT, openai.recommended_transform, openrouter.recommended_transform),
    ):
        expected = [f"openai/{pick}" for pick in ours]
        assert theirs[: len(expected)] == tuple(expected), (
            f"{modality} should lead with the OpenAI picks, prefixed"
        )
        assert len(set(theirs)) == len(theirs), "no duplicates once fallbacks are appended"


def test_a_mirrored_pick_openrouter_does_not_carry_falls_through():
    """gpt-live-transcribe is not on OpenRouter in any form, checked against its catalogue.

    Listing it first therefore has to cost nothing.
    """
    openrouter = SPECS["openrouter"]
    assert openrouter.recommended_transcription[0] == "openai/gpt-live-transcribe"

    offered = ["openai/gpt-transcribe", "openai/gpt-4o-transcribe", "deepgram/nova-3"]
    assert openrouter.recommended(Modality.TRANSCRIPTION, offered) == "openai/gpt-transcribe"

    # And where OpenRouter does carry the pick, it is used unchanged.
    assert (
        openrouter.recommended(Modality.TEXT, ["openai/gpt-5.6-luna", "google/gemini-3.8-flash"])
        == "openai/gpt-5.6-luna"
    )
