"""OpenAI: realtime + batch transcription, and GPT-5.6 transforms via the Responses API.

The only provider here that streams while you speak. Two notes on choices that are not
obvious from the code:

* Transforms go through /v1/responses, not /v1/chat/completions, because Responses is
  OpenAI's recommended path for GPT-5.6 reasoning. OpenRouter and friends stay on
  chat/completions -- same interface in base.py, different transport.
* Prompt caching is opted out of by sending {"mode": "explicit"} with no breakpoints.
  Omitting prompt_cache_options entirely would select *implicit* mode, which caches and
  charges a 1.25x cache-write premium. See CacheMode in base.py for the cost reasoning.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from types import MappingProxyType
from typing import ClassVar

import httpx

from ..config import TARGET_SAMPLE_RATE
from .base import (
    CacheMode,
    Modality,
    ModelInfo,
    ProviderError,
    ReasoningEffort,
    ServiceTier,
    StreamingSession,
    Support,
    TranscribeOptions,
    TranscriptionCapabilities,
    TranscriptionResult,
    TransformCapabilities,
    TransformOptions,
    TransformResult,
)

API_BASE = "https://api.openai.com/v1"
REALTIME_URL = "wss://api.openai.com/v1/realtime"
PROVIDER = "openai"

# Preference ordering for auto-selected models. Discovery still returns everything; this
# only decides what "" (auto) resolves to and what sorts first in the picker. Names, not
# an allowlist -- an unrecognised model is still selectable.
_TRANSCRIBE_RANK = {
    "gpt-live-transcribe": 100,  # recommended for realtime
    "gpt-transcribe": 90,  # recommended for file
    "gpt-realtime-whisper": 70,
    "gpt-4o-transcribe": 60,
    "gpt-4o-mini-transcribe": 55,
    "gpt-4o-transcribe-diarize": 40,
    "whisper-1": 20,
}
_TRANSFORM_RANK = {
    "gpt-5.6-luna": 100,  # efficient tier; the right default for short cleanup passes
    "gpt-5.6-terra": 80,
    "gpt-5.6-sol": 70,
}

# Models that stream deltas during a realtime session, as opposed to only emitting a
# transcript after commit. Used to pick a sensible default for the streaming path.
_STREAMING_MODELS = ("gpt-live-transcribe", "gpt-realtime-whisper")


def _rank(model_id: str, table: dict[str, int]) -> int:
    if model_id in table:
        return table[model_id]
    # Unknown but plausibly newer: rank above legacy, below anything explicitly known.
    for known, score in table.items():
        if model_id.startswith(known):
            return score - 1
    return 0


def _is_transcription_model(model_id: str) -> bool:
    """Name heuristic -- /v1/models exposes no capability metadata.

    Deliberately in one small function so it is easy to correct when naming churns again.
    Being wrong here only affects which list a model appears in; free-text entry always
    works.
    """
    m = model_id.lower()
    return "transcribe" in m or "whisper" in m


def _auth_headers(api_key: str) -> dict[str, str]:
    # Note: no OpenAI-Beta header. It was required for the realtime beta and must be
    # removed for the GA interface.
    return {"Authorization": f"Bearer {api_key}"}


class _OpenAIBase:
    def __init__(self, api_key: str, *, timeout: float = 60.0) -> None:
        if not api_key:
            raise ProviderError("No OpenAI API key configured.", provider=PROVIDER)
        self._api_key = api_key
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE, headers=_auth_headers(self._api_key), timeout=self._timeout
        )

    async def _raw_models(self) -> list[dict]:
        async with self._client() as client:
            resp = await client.get("/models")
        if resp.status_code == 401:
            raise ProviderError("OpenAI rejected the API key.", provider=PROVIDER)
        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenAI /models failed: {resp.status_code} {resp.text[:200]}",
                provider=PROVIDER,
                retryable=resp.status_code >= 500,
            )
        return [m for m in resp.json().get("data", []) if isinstance(m.get("id"), str)]


# --------------------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------------------


class OpenAIRealtimeSession(StreamingSession):
    """A realtime transcription socket.

    Lives entirely on the asyncio thread. Audio arrives via `feed` as 24 kHz mono PCM16
    (the rate the API expects, and the app's internal canonical rate).
    """

    def __init__(self, api_key: str, opts: TranscribeOptions) -> None:
        self._api_key = api_key
        self._opts = opts
        self._ws = None
        self._deltas: asyncio.Queue[str | None] = asyncio.Queue()
        self._final: asyncio.Future[str] | None = None
        self._reader: asyncio.Task | None = None
        self._connected_url: str | None = None

    # -- session config -----------------------------------------------------------------

    def _session_update(self) -> dict:
        transcription: dict[str, object] = {"model": self._opts.model}
        # `keywords` is a first-class field for literal terms -- product names, acronyms,
        # proper nouns. Strictly better than stuffing spellings into the prompt.
        if self._opts.keywords:
            transcription["keywords"] = list(self._opts.keywords)
        if self._opts.prompt:
            transcription["prompt"] = self._opts.prompt
        if self._opts.languages:
            transcription["languages"] = list(self._opts.languages)
        if self._opts.delay:
            transcription["delay"] = self._opts.delay
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": TARGET_SAMPLE_RATE},
                        "transcription": transcription,
                        # None: we commit manually on stop. Server VAD would chop the
                        # recording into turns, which is wrong for push-to-talk.
                        "turn_detection": None,
                    }
                },
            },
        }

    # -- lifecycle ----------------------------------------------------------------------

    async def connect(self) -> None:
        import websockets

        loop = asyncio.get_running_loop()
        self._final = loop.create_future()

        # The docs show ?model= for realtime sessions; older transcription-only examples
        # used ?intent=transcription. Try the documented form, fall back once. Whichever
        # succeeds is recorded so the spike script can report it.
        candidates = [
            f"{REALTIME_URL}?model={self._opts.model}",
            f"{REALTIME_URL}?intent=transcription",
        ]
        last: Exception | None = None
        for url in candidates:
            try:
                self._ws = await websockets.connect(
                    url,
                    additional_headers=_auth_headers(self._api_key),
                    max_size=None,
                )
                self._connected_url = url
                break
            except Exception as exc:  # noqa: BLE001 - handshake failures vary by version
                last = exc
        if self._ws is None:
            raise ProviderError(
                f"Could not open a realtime transcription socket: {last}",
                provider=PROVIDER,
                retryable=True,
            )

        await self._ws.send(json.dumps(self._session_update()))
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                event = json.loads(raw)
                kind = event.get("type", "")
                if kind == "conversation.item.input_audio_transcription.delta":
                    await self._deltas.put(event.get("delta", ""))
                elif kind == "conversation.item.input_audio_transcription.completed":
                    if self._final and not self._final.done():
                        self._final.set_result(event.get("transcript", ""))
                    await self._deltas.put(None)
                elif kind == "error":
                    detail = event.get("error", {}).get("message", "unknown realtime error")
                    if self._final and not self._final.done():
                        self._final.set_exception(
                            ProviderError(detail, provider=PROVIDER, retryable=True)
                        )
                    await self._deltas.put(None)
        except Exception as exc:  # noqa: BLE001
            if self._final and not self._final.done():
                self._final.set_exception(
                    ProviderError(f"Realtime socket closed: {exc}", provider=PROVIDER,
                                  retryable=True)
                )
            await self._deltas.put(None)

    # -- StreamingSession ---------------------------------------------------------------

    async def feed(self, pcm16: bytes) -> None:
        if self._ws is None:
            return
        payload = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16).decode("ascii"),
        }
        await self._ws.send(json.dumps(payload))

    async def deltas(self) -> AsyncIterator[str]:
        while True:
            item = await self._deltas.get()
            if item is None:
                return
            yield item

    async def finish(self) -> TranscriptionResult:
        if self._ws is None or self._final is None:
            raise ProviderError("Realtime session was never connected.", provider=PROVIDER)
        await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        try:
            text = await asyncio.wait_for(self._final, timeout=self._commit_timeout())
        finally:
            await self._close()
        return TranscriptionResult(text=text.strip(), model=self._opts.model, provider=PROVIDER)

    def _commit_timeout(self) -> float:
        # Generous: xhigh delay trades latency for context, so a fixed short timeout would
        # spuriously fail the accurate settings.
        return {"minimal": 15.0, "low": 20.0, "medium": 30.0, "high": 45.0}.get(
            self._opts.delay or "medium", 30.0
        )

    async def abort(self) -> None:
        await self._close()

    async def _close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None


class OpenAITranscription(_OpenAIBase):
    id = PROVIDER
    label = "OpenAI"

    def capabilities(self) -> TranscriptionCapabilities:
        return TranscriptionCapabilities(
            batch=True,
            streaming=True,
            keywords=True,
            prompt=True,
            languages=True,
            delay_tuning=True,
        )

    async def list_models(self) -> list[ModelInfo]:
        models = [
            ModelInfo(
                id=row["id"],
                provider=PROVIDER,
                modality=Modality.TRANSCRIPTION,
                rank=_rank(row["id"], _TRANSCRIBE_RANK),
                created=row.get("created"),
            )
            for row in await self._raw_models()
            if _is_transcription_model(row["id"])
        ]
        return sorted(models, key=lambda m: m.sort_key)

    async def transcribe(self, wav_bytes: bytes, opts: TranscribeOptions) -> TranscriptionResult:
        data: dict[str, str] = {"model": opts.model}
        if opts.prompt:
            data["prompt"] = opts.prompt
        if opts.keywords:
            # One term per line, per the docs' guidance for keyword hints.
            data["keywords"] = "\n".join(opts.keywords)
        if opts.languages:
            data["languages"] = ",".join(opts.languages)
        async with self._client() as client:
            resp = await client.post(
                "/audio/transcriptions",
                data=data,
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            )
        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenAI transcription failed: {resp.status_code} {resp.text[:300]}",
                provider=PROVIDER,
                retryable=resp.status_code >= 500,
            )
        body = resp.json()
        return TranscriptionResult(
            text=(body.get("text") or "").strip(), model=opts.model, provider=PROVIDER
        )

    async def open_stream(self, opts: TranscribeOptions) -> StreamingSession:
        session = OpenAIRealtimeSession(self._api_key, opts)
        await session.connect()
        return session

    @staticmethod
    def default_streaming_model(available: Sequence[ModelInfo]) -> str:
        for pref in _STREAMING_MODELS:
            for m in available:
                if m.id == pref:
                    return m.id
        return available[0].id if available else _STREAMING_MODELS[0]


# --------------------------------------------------------------------------------------
# Transformation
# --------------------------------------------------------------------------------------


class OpenAITransform(_OpenAIBase):
    id = PROVIDER
    label = "OpenAI"

    # OpenAI documents these, so support is definite rather than best-effort. Note the
    # set excludes `minimal` (an OpenRouter value) and includes `max`.
    _EFFORTS = (
        ReasoningEffort.NONE,
        ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM,
        ReasoningEffort.HIGH,
        ReasoningEffort.XHIGH,
        ReasoningEffort.MAX,
    )

    def capabilities(self, model: str | None = None) -> TransformCapabilities:
        # Only the GPT-5.x / o-series accept a reasoning budget; older chat models reject
        # it. When no model is selected yet, report the optimistic baseline.
        reasoning = Support.SUPPORTED
        if model and not (model.startswith("gpt-5") or model.startswith(("o1", "o3", "o4"))):
            reasoning = Support.UNSUPPORTED
        return TransformCapabilities(
            reasoning_effort=reasoning,
            reasoning_efforts=self._EFFORTS if reasoning is Support.SUPPORTED else (),
            priority_processing=Support.SUPPORTED,
            temperature=Support.SUPPORTED,
            cache_control=Support.SUPPORTED,
        )

    async def list_models(self) -> list[ModelInfo]:
        models = [
            ModelInfo(
                id=row["id"],
                provider=PROVIDER,
                modality=Modality.TEXT,
                rank=_rank(row["id"], _TRANSFORM_RANK),
                created=row.get("created"),
                supports_reasoning=row["id"].startswith(("gpt-5", "o1", "o3", "o4")),
            )
            for row in await self._raw_models()
            if not _is_transcription_model(row["id"])
            and self._looks_like_chat_model(row["id"])
        ]
        return sorted(models, key=lambda m: m.sort_key)

    @staticmethod
    def _looks_like_chat_model(model_id: str) -> bool:
        m = model_id.lower()
        if any(x in m for x in ("embedding", "tts", "dall-e", "image", "moderation", "audio")):
            return False
        return m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))

    def _payload(self, system: str, user: str, opts: TransformOptions) -> dict:
        payload: dict[str, object] = {
            "model": opts.model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if opts.reasoning_effort and opts.reasoning_effort != ReasoningEffort.NONE:
            payload["reasoning"] = {"effort": str(opts.reasoning_effort)}
        if opts.service_tier != ServiceTier.STANDARD:
            payload["service_tier"] = str(opts.service_tier)
        if opts.temperature is not None:
            payload["temperature"] = opts.temperature
        if opts.max_output_tokens is not None:
            payload["max_output_tokens"] = opts.max_output_tokens

        # Opting out of prompt caching requires explicit mode with zero breakpoints.
        # Omitting this field would select implicit mode -- caching on, 1.25x write premium.
        if opts.cache_mode == CacheMode.DISABLED:
            payload["prompt_cache_options"] = {"mode": "explicit"}
        else:
            payload["prompt_cache_options"] = {"mode": "implicit"}
        return payload

    # -- live capability discovery ------------------------------------------------------
    #
    # OpenAI's /v1/models returns ids and nothing else -- no capability metadata at all.
    # So rather than hardcode which models accept which parameters (the exact thing that
    # rots), send one minimal request exercising the parameter and read the outcome. The
    # verdict is cached in the catalog essentially forever, because a given model id's
    # capabilities do not change.
    #
    # Cost: a handful of tokens, once per model per parameter.

    _PROBE_PARAMS: ClassVar[Mapping[str, Mapping[str, object]]] = MappingProxyType(
        {
            "reasoning": {"reasoning": {"effort": "low"}},
            "service_tier": {"service_tier": "fast"},
            "temperature": {"temperature": 1.0},
        }
    )

    async def probe_parameter(self, model: str, parameter: str) -> tuple[Support, str]:
        extra = self._PROBE_PARAMS.get(parameter)
        if extra is None:
            return Support.UNKNOWN, f"no probe defined for {parameter!r}"
        payload: dict[str, object] = {
            "model": model,
            "input": [{"role": "user", "content": "ok"}],
            "max_output_tokens": 16,
            # Never let a probe write a cache entry.
            "prompt_cache_options": {"mode": "explicit"},
            **extra,
        }
        try:
            async with self._client() as client:
                resp = await client.post("/responses", json=payload)
        except Exception as exc:  # noqa: BLE001 - network failure is not an answer
            return Support.UNKNOWN, f"probe failed: {exc}"[:160]

        if resp.status_code < 400:
            return Support.SUPPORTED, "accepted"

        detail = resp.text[:300]
        if resp.status_code == 400 and _rejects_parameter(detail, parameter):
            return Support.UNSUPPORTED, detail[:160]
        # 401/429/500 and unrelated 400s say nothing about the parameter itself.
        return Support.UNKNOWN, f"HTTP {resp.status_code}: {detail}"[:160]

    async def transform(
        self, system: str, user: str, opts: TransformOptions
    ) -> TransformResult:
        async with self._client() as client:
            resp = await client.post("/responses", json=self._payload(system, user, opts))
        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenAI transform failed: {resp.status_code} {resp.text[:300]}",
                provider=PROVIDER,
                retryable=resp.status_code >= 500,
            )
        body = resp.json()
        usage = body.get("usage") or {}
        return TransformResult(
            text=_extract_output_text(body),
            model=body.get("model", opts.model),
            provider=PROVIDER,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            reasoning_tokens=(usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
        )


def _extract_output_text(body: dict) -> str:
    """Pull assistant text out of a Responses payload.

    `output_text` is the convenience field; walking `output` is the fallback for shapes
    that include reasoning items before the message.
    """
    if isinstance(body.get("output_text"), str):
        return body["output_text"].strip()
    chunks: list[str] = []
    for item in body.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks).strip()


def _rejects_parameter(body: str, parameter: str) -> bool:
    """Distinguish "this model does not take that parameter" from any other 400.

    Getting this wrong in the permissive direction is the safer failure: an UNKNOWN
    verdict means the UI offers the option with "we'll try" wording, whereas a wrong
    UNSUPPORTED would hide something that actually works.
    """
    low = body.lower()
    if parameter.lower() not in low:
        return False
    return any(
        phrase in low
        for phrase in (
            "unsupported",
            "unknown parameter",
            "unrecognized",
            "not supported",
            "does not support",
            "invalid_request_error",
        )
    )
