"""Fan one microphone tap out to several consumers.

This is the piece that makes provider capability differences invisible. There is exactly
one audio tap; the WAV buffer is always attached, and a streaming sink is attached only when
the selected provider can stream. Switching from OpenAI to OpenRouter changes which sinks
are attached, not how recording works.

User-visible consequence: nothing about the act of recording changes when you change
provider. With a streaming provider the text is ready the moment you stop; with a
batch-only one there is a pause after you stop. Same button, same audio, same fallback if
the network dies mid-sentence.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

Sink = Callable[[bytes], None]

# Coalesce frames into ~100 ms before putting them on the wire. Sending every 20 ms block
# individually means five times the WebSocket messages for no latency benefit, since the
# transcription model works on larger windows anyway.
DEFAULT_STREAM_CHUNK_MS = 100


class AudioTee:
    """Dispatches frames to every attached sink.

    Called from the PortAudio callback thread. A sink that raises is dropped rather than
    allowed to take down capture: losing the transcript stream should not also lose the
    recording.
    """

    def __init__(self) -> None:
        self._sinks: list[Sink] = []
        self._lock = threading.Lock()
        self.sink_errors: list[str] = []

    def add(self, sink: Sink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def remove(self, sink: Sink) -> None:
        with self._lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    def clear(self) -> None:
        with self._lock:
            self._sinks.clear()
        self.sink_errors.clear()

    def __call__(self, pcm16: bytes) -> None:
        # Copy under the lock, dispatch outside it: a slow sink must not block the audio
        # thread's next callback by holding the lock.
        with self._lock:
            sinks = tuple(self._sinks)
        for sink in sinks:
            try:
                sink(pcm16)
            except Exception as exc:  # noqa: BLE001 - isolate one bad sink
                self.sink_errors.append(f"{getattr(sink, 'name', sink)!r}: {exc}")
                self.remove(sink)


class StreamSink:
    """Bridges the audio callback thread to the asyncio loop feeding a live socket.

    The audio thread may not await, and the event loop may not be touched from another
    thread except via call_soon_threadsafe. So frames are coalesced here, handed across the
    boundary once per chunk, and a consumer task on the loop does the actual sending.

    Backpressure is deliberate and lossy: if the network stalls and the queue fills, new
    audio is dropped from the *stream* rather than queued forever. The WAV buffer still has
    every byte, so the batch fallback produces a complete transcript. Growing a queue
    without bound would trade a degraded live transcript for an out-of-memory crash.
    """

    name = "stream"

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        chunk_ms: int = DEFAULT_STREAM_CHUNK_MS,
        sample_rate: int = 24_000,
        max_queued_chunks: int = 200,  # ~20 s of audio in flight before we start dropping
    ) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_queued_chunks)
        self._pending = bytearray()
        self._chunk_bytes = int(sample_rate * 2 * chunk_ms / 1000)
        self.dropped_chunks = 0
        self.sent_chunks = 0

    # -- audio thread -------------------------------------------------------------------

    def __call__(self, pcm16: bytes) -> None:
        self._pending.extend(pcm16)
        while len(self._pending) >= self._chunk_bytes:
            chunk = bytes(self._pending[: self._chunk_bytes])
            del self._pending[: self._chunk_bytes]
            self._loop.call_soon_threadsafe(self._offer, chunk)

    def flush(self) -> None:
        """Push the final partial chunk. Called on stop, before committing the buffer."""
        if self._pending:
            chunk = bytes(self._pending)
            self._pending.clear()
            self._loop.call_soon_threadsafe(self._offer, chunk)
        self._loop.call_soon_threadsafe(self._offer, None)

    # -- event loop ---------------------------------------------------------------------

    def _offer(self, chunk: bytes | None) -> None:
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            # Sentinel must never be dropped, or the consumer would hang forever waiting
            # for an end-of-stream that never arrives.
            if chunk is None:
                self._loop.create_task(self._queue.put(None))
            else:
                self.dropped_chunks += 1

    async def pump(self, send: Callable[[bytes], object]) -> None:
        """Consume the queue until end-of-stream, handing each chunk to `send`."""
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            result = send(chunk)
            if asyncio.iscoroutine(result):
                await result
            self.sent_chunks += 1
