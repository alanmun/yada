"""Input gain was a blind dial: nothing showed whether the microphone was being heard.

Peak rather than RMS, because the question is "am I clipping" -- clipping costs accuracy,
and it lasts a couple of milliseconds, so it needs a hold to be visible at all.
"""

from __future__ import annotations

import struct

import pytest

from yada.audio import peak_level


def test_silence_reads_as_nothing():
    assert peak_level(struct.pack("<8h", *([0] * 8))) == 0.0


def test_an_empty_block_is_not_an_error():
    """The callback can hand over an empty buffer while a stream is starting."""
    assert peak_level(b"") == 0.0


def test_a_half_scale_sample_reads_as_half():
    assert peak_level(struct.pack("<4h", 16384, -100, 0, 5)) == pytest.approx(0.5, abs=0.01)


def test_the_loudest_sample_wins_regardless_of_sign():
    quiet_then_loud = struct.pack("<4h", 10, -30000, 20, 40)
    assert peak_level(quiet_then_loud) == pytest.approx(30000 / 32767, abs=0.001)


def test_the_most_negative_sample_does_not_overflow():
    """abs(-32768) does not fit in int16; widening first is the whole point."""
    assert peak_level(struct.pack("<2h", -32768, 0)) == 1.0


def test_the_level_is_capped_at_one():
    assert peak_level(struct.pack("<1h", -32768)) <= 1.0


# --------------------------------------------------------------------------------------
# The widget
# --------------------------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from yada.ui.widgets import LevelMeter  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_an_inactive_meter_shows_nothing(qapp):
    meter = LevelMeter()
    meter.set_active(True)
    meter.set_level(0.8)
    meter.set_active(False)
    assert meter._level == 0.0 and meter._peak == 0.0, (
        "releasing the microphone must not leave a level on screen"
    )


def test_the_peak_outlives_the_level(qapp):
    """A clip lasts a couple of milliseconds; without a hold it could never be seen."""
    meter = LevelMeter()
    meter.set_active(True)
    meter.set_level(0.9)
    for _ in range(5):
        meter._decay()
    assert meter._level < 0.9
    assert meter._peak > meter._level, "the peak must fall more slowly than the bar"


def test_a_level_decays_to_silence(qapp):
    meter = LevelMeter()
    meter.set_active(True)
    meter.set_level(1.0)
    for _ in range(200):
        meter._decay()
    assert meter._level == 0.0 and meter._peak == 0.0


def test_levels_are_clamped(qapp):
    meter = LevelMeter()
    meter.set_active(True)
    meter.set_level(4.2)
    assert meter._level == 1.0
    meter.set_active(False)
    meter.set_active(True)
    meter.set_level(-1.0)
    assert meter._level == 0.0
