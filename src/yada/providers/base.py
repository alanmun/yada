"""Provider contracts.

Everything in the app is written against these protocols, never against a concrete
provider. Two independent axes -- transcription and transformation -- because a provider
may serve one, the other, or both (OpenAI does both; ElevenLabs would be transcribe-only;
Grok would be transform-only).

Capabilities are declared, not assumed. The pipeline branches on `capabilities()` rather
than on provider identity, so adding a provider never means touching the pipeline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ProviderError(Exception):
    """Base for provider failures that should surface to the user as a readable message."""

    def __init__(self, message: str, *, provider: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class MissingCredentials(ProviderError):
    """No API key configured for this provider."""


class CapabilityUnsupported(ProviderError):
    """Caller asked for something `capabilities()` already said was unavailable."""


# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------


class Modality(StrEnum):
    TRANSCRIPTION = "transcription"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One selectable model.

    Discovered at runtime, never hardcoded -- stale model enums are the specific failure
    mode this app exists to avoid. Fields beyond `id` are best-effort: OpenAI's /v1/models
    exposes no capability metadata, while OpenRouter exposes a great deal. Absent values
    mean "unknown", never "unsupported".
    """

    id: str
    provider: str
    modality: Modality
    label: str | None = None
    context_length: int | None = None
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    supports_reasoning: bool | None = None
    # Per-model parameter support, where the provider reports it. OpenRouter does;
    # OpenAI's /v1/models does not, so this stays empty there and capability questions
    # fall back to the provider-level baseline.
    supported_parameters: tuple[str, ...] = ()
    # Higher sorts first in pickers. Lets a provider surface its own recommendation
    # (e.g. OpenAI's "recommended for new integrations") without hardcoding a list.
    rank: int = 0

    @property
    def display(self) -> str:
        return self.label or self.id


# --------------------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TranscriptionCapabilities:
    """What a transcription provider can actually do.

    `batch` is the floor: every provider must support it, because it is the fallback when
    streaming is unavailable or the socket drops mid-recording.
    """

    batch: bool = True
    streaming: bool = False
    # Native literal-term vocabulary hints. Strictly better than prompt-stuffing when
    # present -- OpenAI's `keywords` field treats entries as literal terms.
    keywords: bool = False
    # Free-form context prompt describing the recording.
    prompt: bool = False
    languages: bool = False
    # Latency/accuracy dial. OpenAI: minimal|low|medium|high|xhigh.
    delay_tuning: bool = False


@dataclass(slots=True)
class TranscribeOptions:
    model: str
    # Uncommon terms and their correct spellings. Single source of truth in config; fanned
    # out to native `keywords` here, to the transform system prompt, and optionally to
    # deterministic find_replace steps.
    keywords: Sequence[str] = ()
    prompt: str | None = None
    languages: Sequence[str] = ()
    delay: str | None = None


@dataclass(slots=True)
class TranscriptionResult:
    text: str
    model: str
    provider: str
    # Populated where the provider reports it (OpenRouter returns seconds/tokens/cost).
    duration_seconds: float | None = None
    cost_usd: float | None = None


@runtime_checkable
class StreamingSession(Protocol):
    """A live transcription socket.

    Owned by the asyncio thread. `feed` is called with 24 kHz mono PCM16 frames as they
    come off the resampler; `deltas` yields partial text as the provider emits it.
    """

    async def feed(self, pcm16: bytes) -> None: ...

    async def deltas(self) -> AsyncIterator[str]: ...

    async def finish(self) -> TranscriptionResult:
        """Commit the buffer, wait for the final transcript, and close."""
        ...

    async def abort(self) -> None:
        """Tear down without waiting for a result."""
        ...


@runtime_checkable
class TranscriptionProvider(Protocol):
    id: str
    label: str

    def capabilities(self) -> TranscriptionCapabilities: ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def transcribe(self, wav_bytes: bytes, opts: TranscribeOptions) -> TranscriptionResult:
        """Batch path. `wav_bytes` is a complete RIFF/WAVE payload."""
        ...

    async def open_stream(self, opts: TranscribeOptions) -> StreamingSession:
        """Streaming path. Raises CapabilityUnsupported if `capabilities().streaming` is False."""
        ...


# --------------------------------------------------------------------------------------
# Transformation
# --------------------------------------------------------------------------------------


class Support(StrEnum):
    """How confident we are that a provider/model honours an option.

    Three states rather than a boolean, because the interesting case is the middle one.
    OpenRouter fronts many providers and cannot always say in advance whether a given
    model honours priority routing or a reasoning effort level. Pretending that is a
    boolean forces a bad choice: hide a option that might work, or show one that silently
    does nothing.

    UNKNOWN means "we will send it and it may be ignored" -- and the UI says exactly that,
    including the billing caveat, rather than implying a guarantee.
    """

    SUPPORTED = "supported"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


class ServiceTier(StrEnum):
    """Speed/cost tier.

    OpenAI renamed 'priority' to 'fast' on 2026-07-30 and bills it at 2x standard for up
    to ~2.5x lower latency; 'priority' is still accepted. OpenRouter's nearest analogue is
    the :nitro suffix / provider.sort=throughput, which makes priority-tier endpoints
    eligible. Providers map this enum onto whatever they actually implement.
    """

    STANDARD = "standard"
    FAST = "fast"
    FLEX = "flex"


class ReasoningEffort(StrEnum):
    """Union of the effort levels seen across providers.

    Not every provider accepts every value -- OpenAI has `max` but not `minimal`,
    OpenRouter the reverse. Never render this enum directly; ask the provider for
    `TransformCapabilities.reasoning_efforts`.
    """

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class CacheMode(StrEnum):
    """Prompt-cache control.

    Not surfaced in the UI, but not a source literal either -- it lives in config so the
    tradeoff can be revisited without a rebuild.

    Why DISABLED is the default: on GPT-5.6+ implicit caching is the API default, so
    sending nothing still incurs a 1.25x cache-write premium on the first request. Reads
    are 0.1x, but the TTL is 30m and dictation is bursty; sporadic use pays the write
    premium repeatedly with no read to amortise it. It is also moot below the 1,024-token
    minimum, which a small system prompt plus a short vocabulary list will not reach.

    Flip to IMPLICIT if the stable prefix grows past ~1,024 tokens AND dictation reliably
    clusters inside 30 minutes; then one write plus nine reads is 2.15x versus 10x.
    """

    DISABLED = "disabled"
    IMPLICIT = "implicit"


@dataclass(frozen=True, slots=True)
class TransformCapabilities:
    """What a transform provider honours, per model where the provider tells us."""

    reasoning_effort: Support = Support.UNSUPPORTED
    # Valid effort values for this provider/model. Empty when reasoning is unsupported.
    reasoning_efforts: tuple[ReasoningEffort, ...] = ()
    # Fast mode / priority routing / :nitro -- whatever the provider calls it.
    priority_processing: Support = Support.UNSUPPORTED
    temperature: Support = Support.SUPPORTED
    cache_control: Support = Support.UNSUPPORTED


# UI copy for capability options, keyed by support level. Lives here so settings widgets
# and providers cannot drift apart on wording.
PRIORITY_LABELS: dict[Support, str] = {
    Support.SUPPORTED: "Use priority processing (faster, costs more)",
    Support.UNKNOWN: "Try to use priority processing if this provider offers one",
    Support.UNSUPPORTED: "Priority processing (not available for this provider)",
}

PRIORITY_TOOLTIPS: dict[Support, str] = {
    Support.SUPPORTED: (
        "Sends this request on the provider's fast tier. Billed at a premium -- OpenAI "
        "charges 2x standard rates for up to ~2.5x lower latency. Dictation transforms "
        "are a few hundred tokens, so the absolute cost stays tiny."
    ),
    Support.UNKNOWN: (
        "We cannot tell in advance whether this model offers a faster tier, so we ask for "
        "one and carry on if it is ignored. If the provider does honour it, expect a "
        "premium on your bill for these requests."
    ),
    Support.UNSUPPORTED: "This provider exposes no faster tier, so the option is ignored.",
}

REASONING_LABELS: dict[Support, str] = {
    Support.SUPPORTED: "Reasoning effort",
    Support.UNKNOWN: "Try to set reasoning effort if this model supports it",
    Support.UNSUPPORTED: "Reasoning effort (not available for this model)",
}

REASONING_TOOLTIPS: dict[Support, str] = {
    Support.SUPPORTED: (
        "Higher effort spends more tokens thinking before answering. Text cleanup rarely "
        "benefits, and reasoning tokens are billed as output -- 'none' is usually right."
    ),
    Support.UNKNOWN: (
        "This model may or may not accept a reasoning budget. We send the request either "
        "way; unsupported values are ignored by the provider. Reasoning tokens, if any "
        "are produced, are billed as output tokens."
    ),
    Support.UNSUPPORTED: "This model does not expose a reasoning budget.",
}


@dataclass(slots=True)
class TransformOptions:
    model: str
    reasoning_effort: ReasoningEffort | None = None
    service_tier: ServiceTier = ServiceTier.STANDARD
    temperature: float | None = None
    max_output_tokens: int | None = None
    cache_mode: CacheMode = CacheMode.DISABLED


@dataclass(slots=True)
class TransformResult:
    text: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    # True when we asked for a faster tier but cannot confirm the provider honoured it.
    # Surfaced in the UI so "why was that slow?" has an answer.
    priority_requested_unconfirmed: bool = False


@runtime_checkable
class TransformProvider(Protocol):
    id: str
    label: str

    def capabilities(self, model: str | None = None) -> TransformCapabilities:
        """Baseline capabilities, refined for `model` when the provider knows more.

        Callers pass the selected model so per-model data (OpenRouter's
        `supported_parameters`) can sharpen UNKNOWN into a definite answer.
        """
        ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def transform(
        self, system: str, user: str, opts: TransformOptions
    ) -> TransformResult: ...


# --------------------------------------------------------------------------------------
# Registry entry
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """How a provider advertises itself to settings UI and the registry.

    `env_var` and `key_label` exist so the credentials pane is generated rather than
    written per provider.
    """

    id: str
    label: str
    key_label: str = "API key"
    env_var: str | None = None
    docs_url: str | None = None
    transcribes: bool = False
    transforms: bool = False
    # Free-text model entry is always permitted; discovery ranks and suggests, never gates.
    notes: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
