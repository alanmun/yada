"""Provider registry.

The only place that knows concrete provider classes exist. Adding a provider means adding
one SPEC entry plus one lazy branch in the builders below -- nothing else in the app
changes, because everything else talks to the protocols in base.py.

Imports are lazy so that a provider needing an optional dependency cannot stop the app
from starting.
"""

from __future__ import annotations

from .base import ProviderSpec, TranscriptionProvider, TransformProvider


def _mirror_openai(picks: tuple[str, ...], *fallbacks: str) -> tuple[str, ...]:
    """OpenRouter's curated picks, led by whatever OpenAI's are.

    OpenRouter is a router: the OpenAI models it carries are the same models, under an
    `openai/` prefix. So the honest recommendation is the one already reasoned about for
    OpenAI, and deriving it means the two cannot drift apart when the OpenAI pick changes.

    Anything OpenRouter does not carry costs nothing, because `ProviderSpec.recommended`
    falls through to the first candidate discovery actually returns. Checked against its
    public catalogue: gpt-live-transcribe is not on OpenRouter in any form, so transcription
    falls through to `openai/gpt-transcribe` -- which is the model measured at 0% word error
    rate on OpenAI's own API. gpt-5.6-luna and gpt-5.6-terra are both carried, so transforms
    mirror exactly.

    The fallbacks are non-OpenAI models for the case where none of the mirrored picks is
    available at all.
    """
    return tuple(dict.fromkeys([f"openai/{pick}" for pick in picks] + list(fallbacks)))


_OPENAI = ProviderSpec(
    id="openai",
    label="OpenAI",
    env_var="OPENAI_API_KEY",
    docs_url="https://developers.openai.com/api/docs",
    transcribes=True,
    transforms=True,
    notes=(
        "Realtime streaming transcription with native keyword hints, plus GPT-5.6 "
        "for transforms. The only provider here that streams while you speak."
    ),
    # Benchmarked against the live API with TTS-generated speech and a known sentence:
    # gpt-live-transcribe is the only transcription model that accepts *both* keywords
    # and delay, reaches 0% word error rate with vocabulary terms supplied, produces a
    # first partial ~0.5s into speech and finalises ~0.7s after you stop.
    # gpt-transcribe is the fallback: equally accurate, no streaming deltas, and it
    # refuses `delay`.
    recommended_transcription=("gpt-live-transcribe", "gpt-transcribe", "gpt-4o-transcribe"),
    # Luna is the efficient tier of the 5.6 family, which is the right shape for a
    # short cleanup pass; Terra is the step up when the pass needs more judgement.
    recommended_transform=("gpt-5.6-luna", "gpt-5.6-terra"),
)

_OPENROUTER = ProviderSpec(
    id="openrouter",
    label="OpenRouter",
    env_var="OPENROUTER_API_KEY",
    docs_url="https://openrouter.ai/docs",
    transcribes=True,
    transforms=True,
    notes=(
        "One key for many models. Batch transcription only -- no realtime socket, so "
        "transcription starts when you stop recording."
    ),
    recommended_transcription=_mirror_openai(
        _OPENAI.recommended_transcription, "openai/whisper-large-v3-turbo"
    ),
    recommended_transform=_mirror_openai(
        _OPENAI.recommended_transform, "google/gemini-3.8-flash", "anthropic/claude-haiku-4.5"
    ),
)

SPECS: dict[str, ProviderSpec] = {"openai": _OPENAI, "openrouter": _OPENROUTER}

# Providers designed for but not yet implemented. Listed so the settings UI can show them
# greyed out rather than pretending the app is OpenAI-only by nature.
PLANNED: dict[str, str] = {
    "elevenlabs": "Batch speech-to-text",
    "groq": "Fast batch Whisper",
    "xai": "Grok, transforms",
    "local": "faster-whisper / whisper.cpp, offline",
}


def transcription_provider_ids() -> list[str]:
    return [s.id for s in SPECS.values() if s.transcribes]


def transform_provider_ids() -> list[str]:
    return [s.id for s in SPECS.values() if s.transforms]


def build_transcriber(provider_id: str, api_key: str) -> TranscriptionProvider:
    if provider_id == "openai":
        from .openai_provider import OpenAITranscription

        return OpenAITranscription(api_key)
    if provider_id == "openrouter":
        from .openrouter import OpenRouterTranscription

        return OpenRouterTranscription(api_key)
    raise KeyError(f"unknown transcription provider: {provider_id!r}")


def build_transformer(provider_id: str, api_key: str) -> TransformProvider:
    if provider_id == "openai":
        from .openai_provider import OpenAITransform

        return OpenAITransform(api_key)
    if provider_id == "openrouter":
        from .openrouter import OpenRouterTransform

        return OpenRouterTransform(api_key)
    raise KeyError(f"unknown transform provider: {provider_id!r}")
