"""Two things that were silently wrong on a user's machine.

Notifications: Windows toasts interrupted for things the chimes and the settings pane
already say, including one warning about a transcription that had in fact fallen back
successfully. They are off by default there now, gated in one place.

Realtime: yada asked for a transcription session with `?model=<transcription model>`, which
that parameter does not accept -- it names a realtime *conversation* model. The server
completed the websocket handshake and only then closed with 4000 invalid_model, so the
`?intent=transcription` fallback beneath it never ran.
"""

from __future__ import annotations

import sys

import pytest

from yada import config
from yada.providers.base import TranscribeOptions
from yada.providers.openai_provider import REALTIME_URL, OpenAIRealtimeSession

pytest.importorskip("PySide6")

from yada.ui.tray import TrayIcon


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------------------


def test_notifications_default_off_on_windows_and_on_elsewhere():
    """The default has to depend on the platform, not on a single hard-coded answer."""
    defaults = config.OutputSettings()
    assert defaults.show_notifications is (sys.platform != "win32")


def test_a_disabled_notification_never_reaches_the_tray(qapp, monkeypatch):
    """Gated in `notify` rather than at the call sites.

    There are half a dozen callers and the next one must not have to remember the setting.
    """
    tray = TrayIcon()
    shown = []
    monkeypatch.setattr(tray._tray, "showMessage", lambda *args, **kwargs: shown.append(args))

    tray.notifications_enabled = False
    tray.notify("yada", "something went wrong", warning=True)
    assert shown == []

    tray.notifications_enabled = True
    tray.notify("yada", "something went wrong", warning=True)
    assert len(shown) == 1, "turning them back on must actually restore them"


def test_notifications_start_enabled_so_none_is_lost_before_settings_load(qapp):
    assert TrayIcon().notifications_enabled is True


# --------------------------------------------------------------------------------------
# Realtime session URL
# --------------------------------------------------------------------------------------


def test_realtime_connects_with_the_transcription_intent(monkeypatch):
    """The URL is the whole bug, so assert the URL.

    `?model=` was tried first for two releases. Because the rejection arrives after the
    handshake, "try the next candidate" could never work -- the failure surfaced mid
    recording as "Live transcription unavailable" for a model that streams fine.
    """
    import asyncio

    urls: list[str] = []

    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

        async def recv(self) -> str:
            return '{"type": "session.updated"}'

        async def close(self) -> None:
            pass

    class FakeWebsockets:
        async def connect(self, url, **_kwargs):
            urls.append(url)
            return FakeSocket()

    monkeypatch.setitem(sys.modules, "websockets", FakeWebsockets())

    opts = TranscribeOptions(model="gpt-live-transcribe")
    session = OpenAIRealtimeSession("sk-test", opts)
    asyncio.run(session.connect())

    assert urls == [f"{REALTIME_URL}?intent=transcription"]
    assert "?model=" not in urls[0], (
        "?model= names a conversation model and rejects every transcription model"
    )


def test_a_rejected_session_fails_connect_rather_than_the_recording(monkeypatch):
    """A refusal must be a connection failure, so the caller can fall back to batch.

    Treating the handshake as success meant the socket looked fine and then died partway
    through a recording, with the audio already gone.
    """
    import asyncio

    from yada.providers.base import ProviderError

    class RejectingSocket:
        async def send(self, payload: str) -> None:
            pass

        async def recv(self) -> str:
            return '{"type": "error", "error": {"message": "invalid_model"}}'

        async def close(self) -> None:
            pass

    class FakeWebsockets:
        async def connect(self, url, **_kwargs):
            return RejectingSocket()

    monkeypatch.setitem(sys.modules, "websockets", FakeWebsockets())

    session = OpenAIRealtimeSession("sk-test", TranscribeOptions(model="nope"))
    with pytest.raises(ProviderError, match="invalid_model"):
        asyncio.run(session.connect())


# --------------------------------------------------------------------------------------
# Model-dependent session fields
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _forget_unsupported_fields():
    """The memo is module state, so tests must not inherit each other's learning."""
    from yada.providers import openai_provider

    openai_provider._UNSUPPORTED_FIELDS.clear()
    yield
    openai_provider._UNSUPPORTED_FIELDS.clear()


class _ScriptedSocket:
    """Answers the session.update with whatever the script says next."""

    def __init__(self, replies: list[str], log: list[dict]) -> None:
        self._replies = list(replies)
        self._log = log
        self.closed = False

    async def send(self, payload: str) -> None:
        import json

        self._log.append(json.loads(payload))

    async def recv(self) -> str:
        return self._replies.pop(0)

    async def close(self) -> None:
        self.closed = True


def test_a_refused_field_is_dropped_and_the_session_retried(monkeypatch):
    """`delay` and `keywords` are model-dependent, and a refusal kills the whole session.

    Measured against the live API: gpt-transcribe and gpt-4o-transcribe refuse `delay`,
    gpt-realtime-whisper refuses `keywords`. yada sent whatever was configured, so simply
    choosing one of those models turned live transcription off, citing a parameter the user
    never set and cannot see.
    """
    import asyncio

    from yada.providers import openai_provider

    sent: list[dict] = []
    refusal = (
        '{"type": "error", "error": {"message": '
        '"The \'delay\' parameter is not supported for this model."}}'
    )
    sockets = [
        _ScriptedSocket([refusal], sent),
        _ScriptedSocket(['{"type": "session.updated"}'], sent),
    ]

    class FakeWebsockets:
        async def connect(self, url, **_kwargs):
            return sockets.pop(0)

    monkeypatch.setitem(sys.modules, "websockets", FakeWebsockets())

    opts = TranscribeOptions(model="picky-model", delay="minimal", keywords=("yada",))
    session = openai_provider.OpenAIRealtimeSession("sk-test", opts)
    asyncio.run(session.connect())

    assert len(sent) == 2, "it must retry rather than give up"
    assert sent[0]["session"]["audio"]["input"]["transcription"]["delay"] == "minimal"
    retried = sent[1]["session"]["audio"]["input"]["transcription"]
    assert "delay" not in retried, "the refused field must be dropped"
    assert retried["keywords"] == ["yada"], "only the refused field goes"
    assert openai_provider._UNSUPPORTED_FIELDS["picky-model"] == {"delay"}


def test_an_unrelated_error_is_not_retried_as_a_field_problem(monkeypatch):
    """Only a named optional field is worth dropping; anything else is a real failure."""
    import asyncio

    from yada.providers import openai_provider
    from yada.providers.base import ProviderError

    sent: list[dict] = []
    socket = _ScriptedSocket(
        ['{"type": "error", "error": {"message": "invalid_model"}}'], sent
    )

    class FakeWebsockets:
        async def connect(self, url, **_kwargs):
            return socket

    monkeypatch.setitem(sys.modules, "websockets", FakeWebsockets())

    session = openai_provider.OpenAIRealtimeSession(
        "sk-test", TranscribeOptions(model="nope", delay="minimal")
    )
    with pytest.raises(ProviderError, match="invalid_model"):
        asyncio.run(session.connect())
    assert len(sent) == 1, "no retry for an error that is not about a field"
    assert openai_provider._UNSUPPORTED_FIELDS == {}


# --------------------------------------------------------------------------------------
# Settings must survive being hand-edited, and must never be silently replaced
# --------------------------------------------------------------------------------------


def test_a_settings_file_with_a_byte_order_mark_still_loads(tmp_path):
    """PowerShell's `Set-Content -Encoding UTF8` writes a BOM, and so do many editors.

    This file is documented as hand-editable, so refusing a BOM meant an ordinary Windows
    edit made it unreadable -- and the old behaviour then overwrote it with defaults.
    """
    from yada import config

    path = tmp_path / "settings.json"
    settings = config.Settings()
    settings.transcription.model = "gpt-live-transcribe"
    config.save(settings, path)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    loaded = config.load(path)
    assert loaded.transcription.model == "gpt-live-transcribe"


def test_an_unreadable_settings_file_is_kept_not_overwritten(tmp_path):
    """Losing a config to a parse error is losing the user's data."""
    from yada import config

    path = tmp_path / "settings.json"
    path.write_text('{"transcription": {"model": "gpt-live-transcribe"', encoding="utf-8")
    original = path.read_bytes()

    loaded = config.load(path)
    assert loaded.transcription.model == "", "defaults are used, so the app still starts"

    kept = list(tmp_path.glob("settings.json.unreadable-*"))
    assert len(kept) == 1, "the unreadable file must be preserved"
    assert kept[0].read_bytes() == original, "preserved verbatim, so it can be recovered"

    # And the app is free to write a fresh one.
    config.save(loaded, path)
    assert config.load(path).transcription.model == ""
