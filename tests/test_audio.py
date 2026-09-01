"""Audio buffering and fan-out.

No microphone needed: the tee and the buffer are pure functions of the bytes handed to them,
and those are the parts where a bug would silently corrupt a recording.
"""

from __future__ import annotations

import asyncio
import io
import wave

from yada.audio.buffer import BYTES_PER_SECOND, WavBuffer
from yada.audio.capture import _to_pcm16
from yada.audio.tee import AudioTee, StreamSink
from yada.config import TARGET_SAMPLE_RATE

FRAME = b"\x01\x02" * 240  # 10 ms at 24 kHz


# --------------------------------------------------------------------------------------
# WavBuffer
# --------------------------------------------------------------------------------------


def test_buffer_accumulates_and_reports_duration():
    buf = WavBuffer()
    assert buf.is_empty
    for _ in range(100):  # 100 x 10 ms = 1 s
        buf.append(FRAME)
    assert not buf.is_empty
    assert buf.nbytes == 100 * len(FRAME)
    assert abs(buf.duration_seconds - 1.0) < 0.001


def test_buffer_produces_a_valid_wav():
    buf = WavBuffer()
    for _ in range(50):
        buf.append(FRAME)
    data = buf.to_wav()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(data)) as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == TARGET_SAMPLE_RATE
        assert wf.readframes(wf.getnframes()) == FRAME * 50


def test_empty_buffer_still_produces_a_valid_header():
    """The session refuses to upload an empty recording, but the header must not be corrupt."""
    data = WavBuffer().to_wav()
    with wave.open(io.BytesIO(data)) as wf:
        assert wf.getnframes() == 0


def test_buffer_cap_truncates_instead_of_growing_without_bound():
    buf = WavBuffer(max_seconds=0.05)  # 50 ms
    for _ in range(100):  # try to write 1 s
        buf.append(FRAME)
    assert buf.truncated is True
    assert buf.nbytes <= int(0.05 * BYTES_PER_SECOND) + len(FRAME)


def test_clear_resets_everything():
    buf = WavBuffer()
    buf.append(FRAME)
    buf.clear()
    assert buf.is_empty and buf.nbytes == 0 and buf.truncated is False


# --------------------------------------------------------------------------------------
# _to_pcm16
# --------------------------------------------------------------------------------------


def test_pcm_conversion_clips_rather_than_wrapping():
    """Wrapping a loud syllable inverts its polarity and sounds like a burst of noise, which
    the transcription model hears as garbage."""
    import numpy as np

    loud = np.array([2.0, -2.0, 0.0], dtype=np.float32)
    out = np.frombuffer(_to_pcm16(loud), dtype="<i2")
    assert out[0] == 32767, "positive overload must clip to full scale"
    assert out[1] == -32767, "negative overload must clip, not wrap positive"
    assert out[2] == 0


def test_pcm_conversion_of_empty_input():
    import numpy as np

    assert _to_pcm16(np.zeros(0, dtype=np.float32)) == b""


# --------------------------------------------------------------------------------------
# AudioTee
# --------------------------------------------------------------------------------------


def test_tee_dispatches_to_every_sink():
    got_a: list[bytes] = []
    got_b: list[bytes] = []
    tee = AudioTee()
    tee.add(got_a.append)
    tee.add(got_b.append)
    tee(FRAME)
    assert got_a == [FRAME] and got_b == [FRAME]


def test_a_failing_sink_is_dropped_and_the_others_keep_working():
    """Losing the live transcript stream must not also lose the recording."""
    good: list[bytes] = []

    def bad(_data):
        raise RuntimeError("socket gone")

    tee = AudioTee()
    tee.add(bad)
    tee.add(good.append)
    tee(FRAME)
    tee(FRAME)
    assert good == [FRAME, FRAME]
    assert len(tee.sink_errors) == 1 and "socket gone" in tee.sink_errors[0]


def test_remove_and_clear():
    got: list[bytes] = []
    tee = AudioTee()
    tee.add(got.append)
    tee.remove(got.append)  # a different bound method object; must not raise
    tee.clear()
    tee(FRAME)
    assert got == []


# --------------------------------------------------------------------------------------
# StreamSink
# --------------------------------------------------------------------------------------


async def test_stream_sink_coalesces_into_chunks():
    loop = asyncio.get_running_loop()
    sink = StreamSink(loop, chunk_ms=100, sample_rate=TARGET_SAMPLE_RATE)
    sent: list[bytes] = []

    # 100 ms at 24 kHz 16-bit = 4800 bytes; each FRAME is 10 ms.
    for _ in range(25):  # 250 ms
        sink(FRAME)
    sink.flush()
    await sink.pump(sent.append)

    assert len(sent) == 3, f"expected two full chunks plus the flushed remainder, got {len(sent)}"
    assert len(sent[0]) == 4800 and len(sent[1]) == 4800
    assert len(sent[2]) == 25 * len(FRAME) - 9600
    assert b"".join(sent) == FRAME * 25, "no audio may be lost or reordered"
    assert sink.sent_chunks == 3


async def test_stream_sink_drops_audio_rather_than_growing_without_bound():
    """A stalled network must degrade the live stream, not exhaust memory. The WAV buffer
    still holds every byte, so the batch fallback is unaffected."""
    loop = asyncio.get_running_loop()
    sink = StreamSink(loop, chunk_ms=10, sample_rate=TARGET_SAMPLE_RATE, max_queued_chunks=3)
    for _ in range(50):
        sink(FRAME)
    await asyncio.sleep(0)  # let the queued call_soon_threadsafe callbacks run
    assert sink.dropped_chunks > 0
    sink.flush()
    await asyncio.sleep(0)

    sent: list[bytes] = []
    await asyncio.wait_for(sink.pump(sent.append), timeout=2.0)
    assert len(sent) <= 4, "the queue must stay bounded"


async def test_flush_terminates_the_pump_even_with_no_audio():
    loop = asyncio.get_running_loop()
    sink = StreamSink(loop)
    sink.flush()
    await asyncio.wait_for(sink.pump(lambda _c: None), timeout=2.0)
    assert sink.sent_chunks == 0
