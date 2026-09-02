"""The picker must never report a model the user did not choose.

Discovery arrives after the settings window opens. In between, the combo was empty and
`current_model()` returned nothing -- so the autosave that follows any edit wrote that
emptiness into settings, and the next refresh selected whatever sorted first. A configured
transform model of gpt-5.6-luna came back as gpt-realtime-2.1-mini that way.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from yada.providers.base import Modality, ModelInfo
from yada.ui.widgets import ModelPicker


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _models(*ids):
    return [ModelInfo(id=i, provider="fake", modality=Modality.TEXT) for i in ids]


def test_an_empty_picker_reports_the_configured_model(qapp):
    """The regression, stated plainly: no list yet must not mean no model."""
    picker = ModelPicker(allow_auto=False)
    picker.set_current("gpt-5.6-luna")
    assert picker.current_model() == "gpt-5.6-luna"


def test_a_configured_model_survives_the_list_arriving(qapp):
    picker = ModelPicker(allow_auto=False)
    picker.set_current("gpt-5.6-luna")
    picker.set_models(_models("gpt-realtime-2.1-mini", "gpt-5.6-luna"), current="gpt-5.6-luna")
    assert picker.current_model() == "gpt-5.6-luna", (
        "the newest model must not displace a configured one"
    )


def test_with_nothing_configured_the_recommendation_wins_over_the_first_row(qapp):
    """Without an "automatic" entry, index 0 is just whatever sorted first."""
    picker = ModelPicker(allow_auto=False)
    picker.set_models(
        _models("gpt-realtime-2.1-mini", "gpt-5.6-luna"),
        current="",
        recommended="gpt-5.6-luna",
    )
    assert picker.current_model() == "gpt-5.6-luna"


def test_the_recommendation_is_marked_but_not_reordered(qapp):
    """Newest-first is what makes a new release visible; the pick is marked in place."""
    picker = ModelPicker(allow_auto=False)
    picker.set_models(
        _models("gpt-realtime-2.1-mini", "gpt-5.6-luna"),
        current="gpt-realtime-2.1-mini",
        recommended="gpt-5.6-luna",
    )
    labels = [picker.combo.itemText(i) for i in range(picker.combo.count())]
    assert labels[0].startswith("gpt-realtime-2.1-mini"), "order is unchanged"
    assert "recommended" in labels[1]
    assert "recommended" not in labels[0]
    assert picker.current_model() == "gpt-realtime-2.1-mini", "marking is not selecting"


def test_a_pinned_model_discovery_did_not_return_stays_selectable(qapp):
    picker = ModelPicker(allow_auto=False)
    picker.set_models(_models("a", "b"), current="something-new")
    assert picker.current_model() == "something-new"


def test_the_automatic_entry_still_means_empty(qapp):
    picker = ModelPicker(allow_auto=True)
    picker.set_models(_models("a", "b"), current="")
    assert picker.current_model() == ""
