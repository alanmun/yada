"""The language dropdown has to actually show its languages.

Reported twice as "set to English and the drop down reveals nothing". The first fix gave the
items explicit size hints and was verified only on Linux, where the bug never reproduced.

Measured on Windows with the real settings window: every row reported 38 pixels while the
popup opened as a six-pixel sliver with a zero-height viewport. Clearing the application
stylesheet fixed it, and re-applying the *same* stylesheet also fixed it -- so the
container's cached geometry was at fault, not any rule. The popup now takes its height from
its own rows, which no cache can undo.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from yada.ui.languages import label_for, sorted_codes
from yada.ui.widgets import CheckableComboBox


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def picker(qapp):
    combo = CheckableComboBox(empty_text="Detect automatically")
    codes = sorted_codes(["en"])
    combo.set_options([(code, label_for(code)) for code in codes], checked=["en"])
    combo.resize(320, 34)
    combo.show()
    qapp.processEvents()
    yield combo
    combo.hidePopup()
    combo.close()


def test_every_known_language_is_offered(picker):
    assert picker.count() >= 30, "the table ships 35 languages; none should be lost"
    assert picker.checked_values() == ["en"]


def test_the_popup_is_tall_enough_to_show_rows(picker, qapp):
    """The symptom was a popup with a zero-height viewport, not a missing model."""
    view = picker.view()
    picker.showPopup()
    qapp.processEvents()

    row_height = view.sizeHintForRow(0)
    assert row_height > 0, "items must have a real height"
    expected = min(picker.count(), picker.maxVisibleItems()) * row_height
    assert view.minimumHeight() >= expected, (
        "showPopup must size the view from its rows rather than trust a cached geometry"
    )


def test_the_popup_height_follows_the_number_of_options(qapp):
    """A short list should not reserve room for twelve rows, or a long one show three."""
    combo = CheckableComboBox()
    try:
        combo.set_options([("a", "Alpha"), ("b", "Beta")], checked=[])
        combo.show()
        qapp.processEvents()
        combo.showPopup()
        qapp.processEvents()
        row = combo.view().sizeHintForRow(0)
        assert combo.view().minimumHeight() >= 2 * row
        assert combo.view().minimumHeight() < 12 * row, "two options, not twelve"
    finally:
        combo.hidePopup()
        combo.close()


def test_an_empty_list_does_not_crash_the_popup(qapp):
    combo = CheckableComboBox(empty_text="Detect automatically")
    try:
        combo.set_options([], checked=[])
        combo.show()
        qapp.processEvents()
        combo.showPopup()  # must not divide by zero or raise
        qapp.processEvents()
        assert combo.count() == 0
    finally:
        combo.hidePopup()
        combo.close()


def test_an_unknown_code_is_kept(picker):
    """A config written by a future version must not lose languages this table lacks."""
    codes = sorted_codes(["en", "zz"])
    assert "zz" in codes
    picker.set_options([(c, label_for(c)) for c in codes], checked=["en", "zz"])
    assert set(picker.checked_values()) == {"en", "zz"}
