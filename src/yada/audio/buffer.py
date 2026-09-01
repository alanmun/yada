"""The recording buffer.

Always present, regardless of provider. It is the floor of the design: batch transcription
needs the whole recording, streaming providers need a fallback when the socket drops
mid-sentence, and a local copy means a failed API call never loses what someone just said.

24 kHz mono 16-bit is 48 KB/s, so a ten-minute dictation is ~29 MB. Cheap enough to hold in
memory, which keeps the hot path free of disk I/O.
"""

from __future__ import annotations

import io
import wave

from ..config import TARGET_SAMPLE_RATE

BYTES_PER_SAMPLE = 2
CHANNELS = 1
BYTES_PER_SECOND = TARGET_SAMPLE_RATE * BYTES_PER_SAMPLE * CHANNELS

# A forgotten recording should not consume the machine. 60 minutes is ~173 MB; well past
# any plausible dictation, and the cap exists to bound a bug rather than to limit the user.
DEFAULT_MAX_SECONDS = 60 * 60


class RecordingTooLong(RuntimeError):
    """The cap was hit. Whatever was captured before the cap is still usable."""


class WavBuffer:
    """Accumulates PCM16 and hands back a complete RIFF/WAVE payload.

    Appends happen on the audio callback thread and must stay cheap: a list append, no
    reallocation of the whole buffer. Assembly into a WAV happens once, on stop.
    """

    def __init__(self, max_seconds: float = DEFAULT_MAX_SECONDS) -> None:
        self._chunks: list[bytes] = []
        self._nbytes = 0
        self._max_bytes = int(max_seconds * BYTES_PER_SECOND)
        self.truncated = False

    # -- audio thread -------------------------------------------------------------------

    def append(self, pcm16: bytes) -> None:
        """Called from the PortAudio callback. Must not block or allocate large objects."""
        if self._nbytes >= self._max_bytes:
            self.truncated = True
            return
        self._chunks.append(pcm16)
        self._nbytes += len(pcm16)

    # -- reads --------------------------------------------------------------------------

    @property
    def nbytes(self) -> int:
        return self._nbytes

    @property
    def duration_seconds(self) -> float:
        return self._nbytes / BYTES_PER_SECOND

    @property
    def is_empty(self) -> bool:
        # Guards the common accident: tapping the hotkey twice sends a 0-byte file and
        # gets a confusing API error instead of being ignored.
        return self._nbytes == 0

    def pcm16(self) -> bytes:
        return b"".join(self._chunks)

    def to_wav(self) -> bytes:
        """A complete WAV file. Both providers' batch paths take this directly."""
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(BYTES_PER_SAMPLE)
            wf.setframerate(TARGET_SAMPLE_RATE)
            wf.writeframes(self.pcm16())
        return out.getvalue()

    def clear(self) -> None:
        self._chunks.clear()
        self._nbytes = 0
        self.truncated = False
