"""What a live refusal teaches has to outlive the process.

`delay` and `keywords` are model-dependent, and a model that refuses one rejects the whole
session. yada learns that from the refusal, drops the field and retries -- but the memo was
process-lifetime, so every launch paid for the lesson again by failing once.
"""

from __future__ import annotations

import sys

import pytest

from yada.config import Settings
from yada.providers.base import Modality, Support, TranscribeOptions
from yada.providers.catalog import ModelCatalog


@pytest.fixture
def catalog(tmp_path):
    return ModelCatalog(tmp_path / "catalog.json")


@pytest.fixture(autouse=True)
def _clean_memo():
    from yada.providers import openai_provider

    openai_provider._UNSUPPORTED_FIELDS.clear()
    openai_provider.set_unsupported_sink(None)
    yield
    openai_provider._UNSUPPORTED_FIELDS.clear()
    openai_provider.set_unsupported_sink(None)


# --------------------------------------------------------------------------------------
# The catalog remembers
# --------------------------------------------------------------------------------------


def test_a_refusal_survives_a_restart(catalog, tmp_path):
    catalog.record_support("openai", "gpt-transcribe", "delay", Support.UNSUPPORTED, "refused")

    reopened = ModelCatalog(tmp_path / "catalog.json")
    assert reopened.unsupported_parameters("openai") == {"gpt-transcribe": {"delay"}}
    assert (
        reopened.entry("openai").support_for("gpt-transcribe", "delay", Support.SUPPORTED)
        is Support.UNSUPPORTED
    ), "what was measured beats what was assumed"


def test_only_refusals_are_reported_as_unsupported(catalog):
    catalog.record_support("openai", "a", "delay", Support.SUPPORTED)
    catalog.record_support("openai", "b", "keywords", Support.UNKNOWN)
    catalog.record_support("openai", "c", "delay", Support.UNSUPPORTED)
    assert catalog.unsupported_parameters("openai") == {"c": {"delay"}}


def test_recording_the_same_answer_twice_is_not_a_write(catalog, monkeypatch):
    catalog.record_support("openai", "m", "delay", Support.UNSUPPORTED)
    saves = []
    monkeypatch.setattr(catalog, "save", lambda: saves.append(1))
    catalog.record_support("openai", "m", "delay", Support.UNSUPPORTED)
    assert saves == [], "an unchanged verdict should not rewrite the file"
    catalog.record_support("openai", "m", "delay", Support.SUPPORTED)
    assert saves == [1], "a changed verdict should"


# --------------------------------------------------------------------------------------
# The provider is told, and tells
# --------------------------------------------------------------------------------------


class _Socket:
    def __init__(self, replies, log):
        self._replies, self._log = list(replies), log

    async def send(self, payload):
        import json

        self._log.append(json.loads(payload))

    async def recv(self):
        return self._replies.pop(0)

    async def close(self):
        pass


def _fake_websockets(sockets, monkeypatch):
    class FakeWebsockets:
        async def connect(self, url, **_kwargs):
            return sockets.pop(0)

    monkeypatch.setitem(sys.modules, "websockets", FakeWebsockets())


def test_a_seeded_refusal_is_never_sent_in_the_first_place(monkeypatch):
    """The point of persisting: the first request of a session already knows better."""
    import asyncio

    from yada.providers import openai_provider

    openai_provider.seed_unsupported({"gpt-transcribe": {"delay"}})
    sent: list[dict] = []
    _fake_websockets([_Socket(['{"type": "session.updated"}'], sent)], monkeypatch)

    session = openai_provider.OpenAIRealtimeSession(
        "sk-test", TranscribeOptions(model="gpt-transcribe", delay="minimal", keywords=("x",))
    )
    asyncio.run(session.connect())

    assert len(sent) == 1, "no failed attempt, so no retry"
    transcription = sent[0]["session"]["audio"]["input"]["transcription"]
    assert "delay" not in transcription
    assert transcription["keywords"] == ["x"], "only the known-refused field is dropped"


def test_a_new_refusal_is_reported_to_the_sink(monkeypatch):
    import asyncio

    from yada.providers import openai_provider

    recorded: list[tuple[str, str, str]] = []
    openai_provider.set_unsupported_sink(lambda m, f, d: recorded.append((m, f, d)))

    refusal = (
        '{"type": "error", "error": {"message": '
        "\"The 'delay' parameter is not supported for this model.\"}}"
    )
    sent: list[dict] = []
    _fake_websockets(
        [_Socket([refusal], sent), _Socket(['{"type": "session.updated"}'], sent)], monkeypatch
    )

    session = openai_provider.OpenAIRealtimeSession(
        "sk-test", TranscribeOptions(model="picky", delay="minimal")
    )
    asyncio.run(session.connect())

    assert [(m, f) for m, f, _ in recorded] == [("picky", "delay")]


def test_a_failing_sink_never_breaks_a_recording(monkeypatch):
    """Persistence is a convenience. Losing it must not cost the dictation."""
    import asyncio

    from yada.providers import openai_provider

    def explode(*_args):
        raise OSError("disk full")

    openai_provider.set_unsupported_sink(explode)
    refusal = (
        '{"type": "error", "error": {"message": '
        "\"The 'delay' parameter is not supported for this model.\"}}"
    )
    sent: list[dict] = []
    _fake_websockets(
        [_Socket([refusal], sent), _Socket(['{"type": "session.updated"}'], sent)], monkeypatch
    )

    session = openai_provider.OpenAIRealtimeSession(
        "sk-test", TranscribeOptions(model="picky", delay="minimal")
    )
    asyncio.run(session.connect())  # must not raise
    assert len(sent) == 2


# --------------------------------------------------------------------------------------
# And the UI stops offering it
# --------------------------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from yada.ui.settings_window import SettingsWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_the_speed_dial_is_disabled_for_a_model_that_refuses_it(qapp):
    window = SettingsWindow(Settings())
    try:
        window.set_transcription_capabilities(delay=Support.SUPPORTED)
        assert window.stt_delay.isEnabled()
        assert not window.stt_delay_note.isVisible()

        window.set_transcription_capabilities(delay=Support.UNSUPPORTED)
        assert not window.stt_delay.isEnabled(), (
            "three of five OpenAI models refuse this; offering it invites a guaranteed failure"
        )
        assert window.stt_delay_note.text()

        # Unknown is not a refusal: send it and see, which is the whole point of tri-state.
        window.set_transcription_capabilities(delay=Support.UNKNOWN)
        assert window.stt_delay.isEnabled()
    finally:
        window.close()


def test_the_catalog_answer_is_what_the_ui_asks(catalog):
    """The UI reads the same store the refusal was written to."""
    catalog.record_support("openai", "gpt-transcribe", "delay", Support.UNSUPPORTED)
    entry = catalog.entry("openai")
    assert entry.support_for("gpt-transcribe", "delay", Support.SUPPORTED) is Support.UNSUPPORTED
    assert entry.support_for("gpt-live-transcribe", "delay", Support.SUPPORTED) is Support.SUPPORTED
    assert Modality.TRANSCRIPTION  # modality is unrelated here, kept explicit
