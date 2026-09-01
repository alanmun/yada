"""OpenRouter: one key, many models. Batch transcription, chat-completions transforms.

The useful contrast with OpenAI: OpenRouter publishes `supported_parameters` per model, so
capability questions get a real answer from the network instead of a name heuristic or a
probe. That is exactly the live-discovery behaviour yada wants everywhere, so this provider
is the reference for how it should feel.

Two gotchas encoded below:

* Send `reasoning: {...}`, never `reasoning_effort`. Sending both returns a 400 on
  reasoning models.
* Effort values differ from OpenAI's: OpenRouter accepts `minimal` and has no `max`.
  Never render a shared enum -- ask `capabilities()`.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable

import httpx

from .base import (
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

API_BASE = "https://openrouter.ai/api/v1"
PROVIDER = "openrouter"

# Attribution headers. Optional, but OpenRouter surfaces them in dashboards and they cost
# nothing to send.
ATTRIBUTION = {
    "HTTP-Referer": "https://github.com/alanmun/yada",
    "X-Title": "yada",
}

# OpenRouter's accepted effort levels. Note `minimal` (absent from OpenAI) and the absence
# of `max` (present in OpenAI).
_EFFORTS = (
    ReasoningEffort.NONE,
    ReasoningEffort.MINIMAL,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
)


class _OpenRouterBase:
    def __init__(self, api_key: str, *, timeout: float = 120.0) -> None:
        if not api_key:
            raise ProviderError("No OpenRouter API key configured.", provider=PROVIDER)
        self._api_key = api_key
        self._timeout = timeout
        # Per-model metadata from the last discovery, so capability answers survive being
        # offline. Seeded from the on-disk catalog at startup.
        self._by_id: dict[str, ModelInfo] = {}

    def seed_models(self, models: Iterable[ModelInfo]) -> None:
        """Hydrate per-model capability data from the cached catalog.

        Without this, a provider constructed while offline would report UNKNOWN for
        everything even though the answers are already on disk.
        """
        for m in models:
            self._by_id[m.id] = m

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {self._api_key}", **ATTRIBUTION},
            timeout=self._timeout,
        )

    async def _fetch_models(self, *, transcription: bool) -> list[ModelInfo]:
        params = {"output_modalities": "transcription"} if transcription else None
        async with self._client() as client:
            resp = await client.get("/models", params=params)
        if resp.status_code == 401:
            raise ProviderError("OpenRouter rejected the API key.", provider=PROVIDER)
        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenRouter /models failed: {resp.status_code} {resp.text[:200]}",
                provider=PROVIDER,
                retryable=resp.status_code >= 500,
            )
        out: list[ModelInfo] = []
        for row in resp.json().get("data", []):
            info = _parse_model(row, transcription=transcription)
            if info is not None:
                out.append(info)
                self._by_id[info.id] = info
        return out


def _to_float(value: object) -> float | None:
    """OpenRouter reports per-token prices as strings; normalise to $/Mtok."""
    try:
        return float(value) * 1_000_000  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_model(row: dict, *, transcription: bool) -> ModelInfo | None:
    mid = row.get("id")
    if not isinstance(mid, str):
        return None
    pricing = row.get("pricing") or {}
    params = tuple(row.get("supported_parameters") or ())
    arch = row.get("architecture") or {}
    out_mods = arch.get("output_modalities") or []
    is_stt = transcription or "transcription" in out_mods
    modality = Modality.TRANSCRIPTION if is_stt else Modality.TEXT
    return ModelInfo(
        id=mid,
        provider=PROVIDER,
        modality=modality,
        label=row.get("name") or mid,
        context_length=row.get("context_length"),
        input_cost_per_mtok=_to_float(pricing.get("prompt")),
        output_cost_per_mtok=_to_float(pricing.get("completion")),
        supports_reasoning="reasoning" in params or None,
        supported_parameters=params,
        # No curated ranking here: OpenRouter's catalogue is thousands of models and any
        # hardcoded preference would be exactly the staleness this app avoids. Sorting is
        # left to the UI (by name, price, or context).
        rank=0,
    )


# --------------------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------------------


class OpenRouterTranscription(_OpenRouterBase):
    id = PROVIDER
    label = "OpenRouter"

    def capabilities(self) -> TranscriptionCapabilities:
        # No realtime socket: transcription begins when recording stops. The recorder's
        # WAV buffer path covers this without any pipeline change.
        return TranscriptionCapabilities(
            batch=True,
            streaming=False,
            keywords=False,
            prompt=True,
            languages=True,
            delay_tuning=False,
        )

    async def list_models(self) -> list[ModelInfo]:
        models = await self._fetch_models(transcription=True)
        return sorted(models, key=lambda m: m.id)

    async def transcribe(self, wav_bytes: bytes, opts: TranscribeOptions) -> TranscriptionResult:
        # Base64 JSON is OpenRouter's native shape, with `format` required. The
        # OpenAI-style multipart form is also accepted but caps at 25 MB.
        payload: dict[str, object] = {
            "model": opts.model,
            "input_audio": {
                "data": base64.b64encode(wav_bytes).decode("ascii"),
                "format": "wav",
            },
        }
        if opts.prompt:
            payload["prompt"] = opts.prompt
        if opts.languages:
            payload["language"] = opts.languages[0]
        # No native keyword field: vocabulary reaches this path via the prompt and via
        # find_replace steps in the transform pipeline instead.

        async with self._client() as client:
            resp = await client.post("/audio/transcriptions", json=payload)
        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenRouter transcription failed: {resp.status_code} {resp.text[:300]}",
                provider=PROVIDER,
                retryable=resp.status_code >= 500,
            )
        body = resp.json()
        usage = body.get("usage") or {}
        return TranscriptionResult(
            text=(body.get("text") or "").strip(),
            model=opts.model,
            provider=PROVIDER,
            duration_seconds=usage.get("seconds"),
            cost_usd=usage.get("cost"),
        )

    async def open_stream(self, opts: TranscribeOptions) -> StreamingSession:
        from .base import CapabilityUnsupported

        raise CapabilityUnsupported(
            "OpenRouter has no realtime transcription socket; use the batch path.",
            provider=PROVIDER,
        )


# --------------------------------------------------------------------------------------
# Transformation
# --------------------------------------------------------------------------------------


class OpenRouterTransform(_OpenRouterBase):
    id = PROVIDER
    label = "OpenRouter"

    async def list_models(self) -> list[ModelInfo]:
        models = await self._fetch_models(transcription=False)
        return sorted(models, key=lambda m: m.id)

    def capabilities(self, model: str | None = None) -> TransformCapabilities:
        """Answered from live per-model metadata where we have it.

        An unrecognised model yields UNKNOWN rather than UNSUPPORTED: the model may be
        brand new, or discovery may not have run. UNKNOWN means the option is offered with
        honest "we'll try" wording, which is better than hiding something that works.
        """
        info = self._by_id.get(model or "")
        if info is None or not info.supported_parameters:
            return TransformCapabilities(
                reasoning_effort=Support.UNKNOWN,
                reasoning_efforts=_EFFORTS,
                priority_processing=Support.UNKNOWN,
                temperature=Support.UNKNOWN,
                cache_control=Support.UNKNOWN,
            )
        params = set(info.supported_parameters)
        reasoning = Support.SUPPORTED if "reasoning" in params else Support.UNSUPPORTED
        return TransformCapabilities(
            reasoning_effort=reasoning,
            reasoning_efforts=_EFFORTS if reasoning is Support.SUPPORTED else (),
            # OpenRouter fronts many upstream providers. :nitro makes priority-tier
            # endpoints eligible but cannot promise one exists for this model, so this
            # stays UNKNOWN by design -- the UI says "try to" and warns about billing.
            priority_processing=Support.UNKNOWN,
            temperature=(
                Support.SUPPORTED if "temperature" in params else Support.UNSUPPORTED
            ),
            cache_control=Support.UNKNOWN,
        )

    def _payload(self, system: str, user: str, opts: TransformOptions) -> dict:
        model = opts.model
        payload: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Ask for cost/token accounting back, so the UI can show what a dictation cost.
            "usage": {"include": True},
        }
        caps = self.capabilities(model)
        if (
            opts.reasoning_effort
            and opts.reasoning_effort != ReasoningEffort.NONE
            and caps.reasoning_effort is not Support.UNSUPPORTED
        ):
            # `reasoning`, never `reasoning_effort` -- sending both 400s.
            payload["reasoning"] = {"effort": str(opts.reasoning_effort)}
        if opts.temperature is not None and caps.temperature is not Support.UNSUPPORTED:
            payload["temperature"] = opts.temperature
        if opts.max_output_tokens is not None:
            payload["max_tokens"] = opts.max_output_tokens
        if opts.service_tier != ServiceTier.STANDARD:
            # Closest analogue to a fast tier: sort by throughput and allow priority-tier
            # endpoints. Equivalent to the :nitro model suffix.
            payload["provider"] = {"sort": "throughput"}
        return payload

    async def transform(
        self, system: str, user: str, opts: TransformOptions
    ) -> TransformResult:
        async with self._client() as client:
            resp = await client.post("/chat/completions", json=self._payload(system, user, opts))
        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenRouter transform failed: {resp.status_code} {resp.text[:300]}",
                provider=PROVIDER,
                retryable=resp.status_code >= 500,
            )
        body = resp.json()
        choices = body.get("choices") or []
        text = ""
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
        usage = body.get("usage") or {}
        return TransformResult(
            text=text,
            model=body.get("model", opts.model),
            provider=PROVIDER,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cost_usd=usage.get("cost"),
            priority_requested_unconfirmed=(opts.service_tier != ServiceTier.STANDARD),
        )
