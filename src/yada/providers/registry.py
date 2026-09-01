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
