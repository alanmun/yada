#!/usr/bin/env python
"""Generate the two notification chimes.

Two requirements shape these:

* They must be *distinguishable without looking* -- one means "your words are ready", the
  other means "the cleanup pass finished". So they differ in contour, not just pitch: the
  transcription chime rises, the transformation chime is a two-note fall-and-settle.
* They must be unobtrusive at 40 dB and not startling at 80. Sine-based with a soft attack
  and an exponential tail; no clicks, which is what you get from a raw gated sine.

Regenerate with:  .venv/bin/python scripts/make_chimes.py
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 48_000  # QSoundEffect resamples happily; 48k keeps the tail clean
ASSETS = Path(__file__).resolve().parent.parent / "src" / "yada" / "assets" / "sounds"


def note(freq: float, duration: float, *, amp: float = 0.5, harmonic: float = 0.18) -> np.ndarray:
    """A single soft note: sine plus a quiet octave, with a click-free envelope."""
    t = np.linspace(0.0, duration, int(SAMPLE_RATE * duration), endpoint=False)
    wave_ = np.sin(2 * np.pi * freq * t) + harmonic * np.sin(2 * np.pi * freq * 2 * t)

    # 6 ms raised-cosine attack removes the click a hard start produces; exponential decay
    # is what makes it read as a chime rather than a beep.
    attack = int(SAMPLE_RATE * 0.006)
    env = np.exp(-t * (4.5 / duration))
    if attack > 0:
        ramp = 0.5 * (1 - np.cos(np.linspace(0, np.pi, attack)))
        env[:attack] *= ramp
    return amp * wave_ * env


def sequence(notes: list[tuple[float, float, float]], *, gap: float = 0.0) -> np.ndarray:
    """Lay notes end to end, overlapping slightly so they blend instead of stuttering."""
    parts: list[np.ndarray] = []
    for freq, duration, delay in notes:
        seg = note(freq, duration)
        if delay > 0:
            seg = np.concatenate([np.zeros(int(SAMPLE_RATE * delay)), seg])
        parts.append(seg)
    length = max(len(p) for p in parts)
    out = np.zeros(length)
    for p in parts:
        out[: len(p)] += p
    if gap:
        out = np.concatenate([out, np.zeros(int(SAMPLE_RATE * gap))])
    return out


def write(path: Path, samples: np.ndarray) -> None:
    peak = float(np.max(np.abs(samples))) or 1.0
    # Normalise to -3 dBFS: loud enough to hear over music, quiet enough not to jolt.
    normalised = samples / peak * 0.707
    pcm = np.clip(normalised, -1.0, 1.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes((pcm * 32767).astype("<i2").tobytes())
    print(f"  {path.name}: {len(pcm) / SAMPLE_RATE * 1000:.0f} ms")


def main() -> None:
    print("Writing chimes:")
    # Listening: a single short low-to-mid tap. Deliberately the plainest of the three --
    # it fires every time you start talking, so it must not be a fanfare. Distinct from
    # the other two by being one note rather than two.
    write(ASSETS / "listening.wav", sequence([(880.00, 0.13, 0.0)]))
    # Transcription: rising perfect fifth (E6 -> B6). Reads as "here it is".
    write(
        ASSETS / "transcription.wav",
        sequence([(1318.51, 0.10, 0.0), (1975.53, 0.20, 0.075)]),
    )
    # Transformation: falling major third settling on a longer note (B6 -> G6).
    # Distinct contour, so the two are unmistakable without paying attention.
    write(
        ASSETS / "transformation.wav",
        sequence([(1975.53, 0.09, 0.0), (1567.98, 0.26, 0.070)]),
    )


if __name__ == "__main__":
    main()
