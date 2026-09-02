"""Provider registry.

The only place that knows concrete provider classes exist. Adding a provider means adding
one SPEC entry plus one lazy branch in the builders below -- nothing else in the app
changes, because everything else talks to the protocols in base.py.

Imports are lazy so that a provider needing an optional dependency cannot stop the app
from starting.
"""

from __future__ import annotations

from .base import ProviderSpec, TranscriptionProvider, TransformProvider

SPECS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
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
    ),
    "openrouter": ProviderSpec(
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
        # OpenRouter lists 19 transcription models and several hundred text ones, so these
        # are picked from its public catalogue rather than measured end to end. gpt-transcribe
        # is the same model measured at 0% word error rate on OpenAI's own API, which makes
        # it the pick that is actually backed by evidence; the rest are ordered fallbacks.
        recommended_transcription=(
            "openai/gpt-transcribe",
            "openai/gpt-4o-transcribe",
            "openai/whisper-large-v3-turbo",
        ),
        recommended_transform=(
            "openai/gpt-5.6-luna",
            "google/gemini-3.8-flash",
            "anthropic/claude-haiku-4.5",
        ),
    ),
}

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
