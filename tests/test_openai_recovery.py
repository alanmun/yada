"""Compatibility recovery must preserve audio and stop on unrelated API errors."""

import httpx
import pytest

from yada.providers.base import ProviderError, TranscribeOptions
from yada.providers.openai_provider import OpenAITranscription, _unsupported_field


@pytest.mark.parametrize(
    "error",
    [
        {"code": "unsupported_parameter", "param": "session.audio.input.transcription.delay"},
        {"code": "invalid_parameter", "param": "delay", "message": "This is not supported"},
        {"message": "Unknown parameter: 'delay'"},
        {"message": "The 'delay' parameter is not supported for this model."},
    ],
)
def test_recognizes_structured_and_legacy_refusals(error):
    assert _unsupported_field(error, {"delay": "minimal"}) == "delay"
    assert _unsupported_field(error, {}) is None


@pytest.mark.parametrize(
    "error",
    [
        {"code": "invalid_parameter", "param": "delay", "message": "Invalid value"},
        {"code": "unsupported_parameter", "param": "model"},
        {"code": "unsupported_parameter", "param": "unrelated.delay"},
        {"type": "invalid_request_error", "param": "delay"},
        {"code": "unsupported_parameter", "param": None},
    ],
)
def test_does_not_guess_at_invalid_values_or_required_fields(error):
    assert _unsupported_field(error, {"delay": "minimal", "model": "m"}) is None


async def test_batch_retries_same_audio_dropping_only_named_optional_fields(monkeypatch):
    requests = []
    audio = b"unique audio bytes"

    def respond(request):
        requests.append(request)
        assert audio in request.content
        assert b'name="model"' in request.content
        if len(requests) <= 2:
            param = ("keywords", "languages")[len(requests) - 1]
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "unsupported_parameter",
                        "param": param,
                        "message": "Parameter is no longer supported",
                    }
                },
            )
        return httpx.Response(200, json={"text": "Recovered"})

    provider = OpenAITranscription("test")
    monkeypatch.setattr(
        provider,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            transport=httpx.MockTransport(respond),
        ),
    )
    options = TranscribeOptions(model="m", keywords=("yada",), languages=("en",))
    result = await provider.transcribe(audio, options)
    assert result.text == "Recovered"
    assert len(requests) == 3
    assert b'name="keywords"' not in requests[1].content
    assert b'name="languages"' in requests[1].content
    assert b'name="languages"' not in requests[2].content
    assert options.keywords == ("yada",)
    assert options.languages == ("en",)


@pytest.mark.parametrize(
    "status,param,code",
    [
        (400, "model", "unsupported_parameter"),
        (400, "languages", "invalid_parameter"),
        (401, "languages", "unsupported_parameter"),
        (429, "languages", "unsupported_parameter"),
    ],
)
async def test_unrecoverable_errors_include_diagnostics_without_retries(
    monkeypatch,
    status,
    param,
    code,
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"x-request-id": "req-test"},
            json={
                "error": {
                    "code": code,
                    "param": param,
                    "type": "invalid_request_error",
                    "message": "Rejected request",
                }
            },
        )

    provider = OpenAITranscription("test")
    monkeypatch.setattr(
        provider,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            transport=httpx.MockTransport(respond),
        ),
    )
    with pytest.raises(ProviderError) as caught:
        await provider.transcribe(b"audio", TranscribeOptions(model="m", languages=("en",)))
    assert len(requests) == 1
    for detail in ("model=m", f"param={param}", code, "req-test", "Rejected request"):
        assert detail in str(caught.value)
    assert caught.value.retryable is (status == 429)


async def test_repeated_refusal_stops_after_field_removed(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "unsupported_parameter",
                    "param": "languages",
                }
            },
        )

    provider = OpenAITranscription("test")
    monkeypatch.setattr(
        provider,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            transport=httpx.MockTransport(respond),
        ),
    )
    with pytest.raises(ProviderError):
        await provider.transcribe(b"audio", TranscribeOptions(model="m", languages=("en",)))
    assert len(requests) == 2


async def test_live_model_uses_file_model_for_saved_audio(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        assert b"gpt-transcribe" in request.content
        assert b"gpt-live-transcribe" not in request.content
        assert b"saved audio" in request.content
        return httpx.Response(200, json={"text": "rescued dictation"})

    provider = OpenAITranscription("test")
    monkeypatch.setattr(
        provider,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            transport=httpx.MockTransport(respond),
        ),
    )
    opts = TranscribeOptions(model="gpt-live-transcribe")
    result = await provider.transcribe(b"saved audio", opts)
    assert result.model == "gpt-transcribe"
    assert result.text == "rescued dictation"
    assert opts.model == "gpt-live-transcribe"
    assert len(requests) == 1


async def test_cancelled_session_confirmation_closes_socket(monkeypatch):
    import asyncio
    import json
    import sys

    from yada.providers.openai_provider import OpenAIRealtimeSession

    waiting = asyncio.Event()

    class Socket:
        closed = False

        async def send(self, payload):
            assert json.loads(payload)["type"] == "session.update"

        async def recv(self):
            waiting.set()
            await asyncio.Future()

        async def close(self):
            self.closed = True

    socket = Socket()

    class Websockets:
        async def connect(self, *args, **kwargs):
            return socket

    monkeypatch.setitem(sys.modules, "websockets", Websockets())
    session = OpenAIRealtimeSession("test", TranscribeOptions(model="gpt-live-transcribe"))
    task = asyncio.create_task(session.connect())
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed
