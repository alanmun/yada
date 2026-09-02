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
