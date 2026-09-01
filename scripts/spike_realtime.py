#!/usr/bin/env python
"""Resolve the open questions about OpenAI's realtime transcription API against a real key.

The docs do not state whether a transcription-only session connects via
`?model=<model>` or the older `?intent=transcription`, so the provider tries both. This
script reports which actually works, plus a few other things worth knowing before the
pipeline depends on them.

Run:  OPENAI_API_KEY=sk-... .venv/bin/python scripts/spike_realtime.py

Sends ~2 seconds of synthetic audio. Costs a fraction of a cent.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from yada.config import TARGET_SAMPLE_RATE
from yada.providers.base import TranscribeOptions
from yada.providers.openai_provider import (
    REALTIME_URL,
    OpenAIRealtimeSession,
    OpenAITranscription,
    _auth_headers,
)

KEY = os.environ.get("OPENAI_API_KEY", "")


def tone_pcm16(seconds: float = 2.0, freq: float = 220.0) -> bytes:
    """Synthetic audio. Will not transcribe to words -- we are testing the transport."""
    n = int(TARGET_SAMPLE_RATE * seconds)
    return b"".join(
        struct.pack("<h", int(0.2 * 32767 * math.sin(2 * math.pi * freq * i / TARGET_SAMPLE_RATE)))
        for i in range(n)
    )


async def probe_connect_url(model: str) -> None:
    """Which query-parameter form does a transcription session accept?"""
    import websockets

    print(f"\n=== realtime connect form (model={model}) ===")
    for url in (f"{REALTIME_URL}?model={model}", f"{REALTIME_URL}?intent=transcription"):
        label = url.split("?", 1)[1]
        try:
            ws = await websockets.connect(url, additional_headers=_auth_headers(KEY), max_size=None)
        except Exception as exc:  # noqa: BLE001
            print(f"  ?{label:32} FAILED  {type(exc).__name__}: {str(exc)[:110]}")
            continue
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=5.0)
            kind = json.loads(first).get("type", "?")
            print(f"  ?{label:32} OK      first server event: {kind}")
        except TimeoutError:
            print(f"  ?{label:32} OK      (connected; no greeting within 5s)")
        finally:
            await ws.close()


async def probe_models() -> list[str]:
    print("\n=== discovered transcription models ===")
    provider = OpenAITranscription(KEY)
    models = await provider.list_models()
    for m in models:
        print(f"  rank {m.rank:4}  {m.id}")
    if not models:
        print("  (none matched the name heuristic — worth revisiting _is_transcription_model)")
    return [m.id for m in models]


async def probe_stream(model: str) -> None:
    """Full streaming round trip, including whether `keywords` is accepted."""
    print(f"\n=== streaming round trip ({model}) ===")
    opts = TranscribeOptions(
        model=model,
        keywords=["Troutwood", "yada", "DynamoDB"],
        prompt="A developer dictating notes about software.",
        languages=["en"],
        delay="minimal",
    )
    session = OpenAIRealtimeSession(KEY, opts)
    t0 = time.perf_counter()
    try:
        await session.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"  connect failed: {exc}")
        return
    print(f"  connected via {session._connected_url}  ({time.perf_counter() - t0:.2f}s)")

    audio = tone_pcm16()
    chunk = TARGET_SAMPLE_RATE // 10 * 2  # 100 ms of 16-bit mono
    for i in range(0, len(audio), chunk):
        await session.feed(audio[i : i + chunk])
    print(f"  fed {len(audio) / 2 / TARGET_SAMPLE_RATE:.1f}s of audio")

    t1 = time.perf_counter()
    try:
        result = await session.finish()
        print(f"  commit -> final transcript in {time.perf_counter() - t1:.2f}s")
        print(f"  transcript: {result.text!r}  (empty is expected for a pure tone)")
    except Exception as exc:  # noqa: BLE001
        print(f"  finish failed: {exc}")
        await session.abort()


async def probe_transform_params(model: str = "gpt-5.6-luna") -> None:
    """Confirm the capability prober classifies real responses correctly."""
    from yada.providers.openai_provider import OpenAITransform

    print(f"\n=== capability probes ({model}) ===")
    t = OpenAITransform(KEY)
    for param in ("reasoning", "service_tier", "temperature"):
        support, detail = await t.probe_parameter(model, param)
        print(f"  {param:14} -> {support!s:12} {detail[:70]}")


async def main() -> int:
    if not KEY:
        print("Set OPENAI_API_KEY first.")
        return 1
    models = await probe_models()
    stream_model = next(
        (m for m in models if m in ("gpt-live-transcribe", "gpt-realtime-whisper")),
        models[0] if models else "gpt-live-transcribe",
    )
    await probe_connect_url(stream_model)
    await probe_stream(stream_model)
    await probe_transform_params()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
