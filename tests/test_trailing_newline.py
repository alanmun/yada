"""A trailing line break on whatever leaves yada.

Applied on the way out only. Putting it in the transcript would carry it into the stored
recording, the live panel, and the text a transform is asked to clean up -- none of which is
what "the paste ends a line" means.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from yada.config import Settings
from yada.pipeline.session import SessionResult, SessionState


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def yada(qapp, tmp_path, monkeypatch):
    import yada.config as cfg
    from yada.app import YadaApp

    monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr("yada.providers.catalog.cache_dir", lambda: tmp_path)
    app = YadaApp(qapp)
    yield app
    app.overlay.dismiss()
    app.overlay.deleteLater()
    app.tray.hide()
    app.async_thread.stop()
    qapp.processEvents()


def test_it_is_on_without_being_configured():
    assert Settings().output.append_newline is True


def test_the_break_is_added(yada):
    yada.settings.output.append_newline = True
    assert yada._outgoing("what I said") == "what I said\n"


def test_turning_it_off_leaves_the_text_alone(yada):
    yada.settings.output.append_newline = False
    assert yada._outgoing("what I said") == "what I said"


def test_an_existing_break_is_not_doubled(yada):
    """Some models already end on one; two blank lines is not what anyone asked for."""
    yada.settings.output.append_newline = True
    assert yada._outgoing("already ends\n") == "already ends\n"


def test_empty_text_gets_nothing(yada):
    yada.settings.output.append_newline = True
    assert yada._outgoing("") == ""


def test_every_way_out_goes_through_it(yada, qapp, monkeypatch):
    """Four call sites copy text outward, and they must not disagree."""
    from yada import app as app_module

    copied: list[str] = []
    monkeypatch.setattr(app_module, "copy", lambda text: (copied.append(text), (True, None))[1])
    monkeypatch.setattr(yada.paste_backend, "paste", lambda: (True, None))
    yada.settings.output.append_newline = True

    yada._deliver("pasted", None)
    yada._copy_only("from the recordings tab")

    yada.tray.set_result(
        SessionResult(
            transcript="tray copy",
            final_text="tray copy",
            duration_seconds=1.0,
            streamed=True,
            transform=None,
            warnings=[],
        )
    )
    yada._copy_last_transcript()

    yada.settings.output.paste_mode = "off"
    yada.settings.output.always_copy_to_clipboard = True
    yada.bridge.on_finished(
        SessionResult(
            transcript="finished",
            final_text="finished",
            duration_seconds=1.0,
            streamed=True,
            transform=None,
            warnings=[],
        )
    )
    qapp.processEvents()

    assert copied == [
        "pasted\n",
        "from the recordings tab\n",
        "tray copy\n",
        "finished\n",
    ], "every exit adds the break, and none of them adds two"


def test_the_transcript_itself_is_untouched(yada, qapp):
    """What the overlay shows, and what a recording stores, is what was said."""
    yada.settings.output.append_newline = True
    result = SessionResult(
        transcript="no break here",
        final_text="no break here",
        duration_seconds=1.0,
        streamed=True,
        transform=None,
        warnings=["something to keep the panel up"],
    )
    yada.bridge.on_finished(result)
    qapp.processEvents()

    assert result.transcript == "no break here", "the result object is not rewritten"
    assert not yada.overlay.text.text().endswith("\n")
    assert yada.tray.last_transcript == "no break here"
    assert yada.session.state is not SessionState.RECORDING
