"""The dictation state machine.

    IDLE ──toggle──> RECORDING ──toggle──> TRANSCRIBING ──> TRANSFORMING ──> IDLE
                                                 │                │
                                                 └── chime 1      └── chime 2

Deliberately free of Qt and of provider construction: everything it needs arrives through
`SessionDeps`, which makes the whole flow testable with fakes and no audio hardware.

Two resilience properties worth stating, because they are the difference between a tool you
trust and one you don't:

* **The recording is always buffered locally.** If the realtime socket fails to open, drops
  mid-sentence, or returns nothing, the batch path runs on the same audio. A network blip
  costs a second, not the dictation.
* **A failed transform still yields the transcript.** You get your words back even when the
  cleanup pass fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ..audio import AudioCapture, AudioDeviceError, AudioTee, StreamSink, WavBuffer
from ..config import Settings
from ..providers.base import (
    TranscribeOptions,
    TranscriptionProvider,
    TransformOptions,
    TransformProvider,
)
from .transform import TransformOutcome, run_steps


class SessionState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    TRANSFORMING = "transforming"


class Stage(StrEnum):
    # Fires the moment recording starts. Without it, pressing the shortcut produced no
    # feedback at all until transcription finished -- and if anything was misconfigured,
    # no feedback ever, which is indistinguishable from a shortcut that is not registered.
    LISTENING = "listening"
    TRANSCRIPTION = "transcription"
    TRANSFORMATION = "transformation"


@dataclass(slots=True)
class SessionResult:
    transcript: str
    final_text: str
    duration_seconds: float
    streamed: bool
    transform: TransformOutcome | None = None
    warnings: list[str] = field(default_factory=list)


class SessionEvents(Protocol):
    """Called from the asyncio thread; implementations must marshal to the UI themselves."""

    def on_state(self, state: SessionState) -> None: ...
    def on_partial(self, text: str) -> None: ...
    def on_finished(self, result: SessionResult) -> None: ...
    def on_error(self, message: str) -> None: ...
    def on_warning(self, message: str) -> None: ...


@dataclass(slots=True)
class SessionDeps:
    """Everything the session needs, injected so nothing here builds providers or touches Qt."""

    settings: Callable[[], Settings]
    # None means "not configured" -- the session reports that rather than crashing.
    transcriber: Callable[[], tuple[TranscriptionProvider, TranscribeOptions] | None]
    transformer: Callable[[], tuple[TransformProvider, TransformOptions] | None]
    events: SessionEvents
    chime: Callable[[Stage], None]
    deliver: Callable[[str, Stage], None]


class DictationSession:
    def __init__(self, loop: asyncio.AbstractEventLoop, deps: SessionDeps) -> None:
        self._loop = loop
        self._deps = deps
        self._state = SessionState.IDLE
        self._buffer: WavBuffer | None = None
        self._tee: AudioTee | None = None
        self._capture: AudioCapture | None = None
        self._stream_sink: StreamSink | None = None
        self._stream_session = None
        self._pump: asyncio.Task | None = None
        self._delta_reader: asyncio.Task | None = None
        self._partial = ""
        self._started_at = 0.0
        self._busy = asyncio.Lock()

    @property
    def state(self) -> SessionState:
        return self._state

    def _set_state(self, state: SessionState) -> None:
        self._state = state
        self._deps.events.on_state(state)

    # -- entry point --------------------------------------------------------------------

    def toggle(self) -> None:
        """Thread-safe. Called from the hotkey thread, the tray, and the IPC server."""
        asyncio.run_coroutine_threadsafe(self.toggle_async(), self._loop)

    async def toggle_async(self) -> None:
        if self._state is SessionState.IDLE:
            await self._start()
        elif self._state is SessionState.RECORDING:
            await self._stop_and_process()
        else:
            # Ignoring rather than queueing: starting a second recording while the first is
            # still being transcribed would need two independent pipelines, and the wait is
            # normally a second or two. Revisit if that turns out to grate.
            self._deps.events.on_warning("Still finishing the last dictation…")

    # -- recording ----------------------------------------------------------------------

    async def _start(self) -> None:
        settings = self._deps.settings()
        configured = self._deps.transcriber()
        if configured is None:
            # Prefixed so the app can recognise this specific case and open Settings. A
            # tray notification is useless here: Windows 11 hides new tray icons behind
            # the overflow arrow, so pressing the shortcut looked like it did nothing at
            # all -- which is exactly how it was reported.
            self._deps.events.on_error(
                "NOT_CONFIGURED: No transcription provider is configured yet. "
                "Add an API key on the Providers tab."
            )
            return
        provider, opts = configured

        self._buffer = WavBuffer()
        self._tee = AudioTee()
        self._tee.add(self._buffer.append)
        self._partial = ""
        self._stream_sink = None
        self._stream_session = None

        # Attach the streaming sink only if this provider can stream and the user wants it.
        # Failure here is not fatal -- the buffer path covers it.
        if settings.transcription.prefer_streaming and provider.capabilities().streaming:
            await self._try_open_stream(provider, opts)

        self._capture = AudioCapture(
            self._tee,
            device=settings.audio.device,
            gain=settings.audio.input_gain,
        )
        try:
            self._capture.start()
        except AudioDeviceError as exc:
            await self._abort_stream()
            self._capture = None
            self._deps.events.on_error(str(exc))
            return

        self._started_at = time.monotonic()
        self._set_state(SessionState.RECORDING)
        # After the microphone is actually open, so the chime means "listening", not
        # "tried to listen".
        self._deps.chime(Stage.LISTENING)

    async def _try_open_stream(
        self, provider: TranscriptionProvider, opts: TranscribeOptions
    ) -> None:
        try:
            session = await provider.open_stream(opts)
        except Exception as exc:  # noqa: BLE001 - any failure just means batch instead
            self._deps.events.on_warning(
                f"Live transcription unavailable, will transcribe on stop ({exc})"
            )
            return
        self._stream_session = session
        sink = StreamSink(self._loop)
        self._stream_sink = sink
        assert self._tee is not None
        self._tee.add(sink)
        self._pump = asyncio.create_task(sink.pump(session.feed))
        self._delta_reader = asyncio.create_task(self._read_deltas(session))

    async def _read_deltas(self, session) -> None:
        try:
            async for delta in session.deltas():
                self._partial += delta
                self._deps.events.on_partial(self._partial)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a lost delta stream still leaves the final result
            return

    # -- stop and process ---------------------------------------------------------------

    async def _stop_and_process(self) -> None:
        if self._busy.locked():
            return
        async with self._busy:
            warnings: list[str] = []
            duration = time.monotonic() - self._started_at

            if self._capture is not None:
                self._capture.stop()
                if self._capture.last_error:
                    warnings.append(f"Audio warning: {self._capture.last_error}")
                self._capture = None

            buffer = self._buffer
            if buffer is None or buffer.is_empty:
                await self._abort_stream()
                self._reset()
                self._deps.events.on_warning("Nothing was recorded.")
                return
            if buffer.truncated:
                warnings.append("Recording hit the length cap and was truncated.")

            self._set_state(SessionState.TRANSCRIBING)
            transcript, streamed, stt_warnings = await self._transcribe(buffer)
            warnings.extend(stt_warnings)

            if not transcript:
                self._reset()
                # The warnings are the only record of *why* nothing came back -- a model
                # that cannot do batch, a refused session, a provider error. Reporting
                # "produced no text" on its own sent a user hunting through a working
                # microphone for a fault that was in the request.
                detail = " ".join(warnings).strip()
                self._deps.events.on_error(f"Transcription produced no text. {detail}".strip())
                return

            self._deps.chime(Stage.TRANSCRIPTION)
            settings = self._deps.settings()
            if settings.output.paste_mode == "after_transcription":
                self._deps.deliver(transcript, Stage.TRANSCRIPTION)

            final = transcript
            outcome: TransformOutcome | None = None
            if settings.transform.enabled and settings.transform.steps:
                self._set_state(SessionState.TRANSFORMING)
                outcome = await self._transform(transcript, settings)
                final = outcome.text
                if outcome.first_error:
                    warnings.append(f"Transform: {outcome.first_error}")
                self._deps.chime(Stage.TRANSFORMATION)
                if settings.output.paste_mode == "after_transformation":
                    self._deps.deliver(final, Stage.TRANSFORMATION)
            elif settings.output.paste_mode == "after_transformation":
                # Asking to paste after transform with no transform configured would
                # otherwise mean nothing ever gets pasted.
                self._deps.deliver(final, Stage.TRANSCRIPTION)
                warnings.append("No transform is configured, so the transcript was used.")

            self._reset()
            self._deps.events.on_finished(
                SessionResult(
                    transcript=transcript,
                    final_text=final,
                    duration_seconds=duration,
                    streamed=streamed,
                    transform=outcome,
                    warnings=warnings,
                )
            )

    async def _transcribe(self, buffer: WavBuffer) -> tuple[str, bool, list[str]]:
        """Prefer the already-open stream; fall back to batch on any failure."""
        warnings: list[str] = []
        if self._stream_sink is not None and self._stream_session is not None:
            self._stream_sink.flush()
            if self._pump is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._pump, timeout=10.0)
            if self._stream_sink.dropped_chunks:
                warnings.append(
                    f"{self._stream_sink.dropped_chunks} audio chunks were dropped from the "
                    "live stream; the saved recording is still complete."
                )
            try:
                result = await self._stream_session.finish()
                await self._cancel_delta_reader()
                if result.text:
                    return result.text, True, warnings
                warnings.append("Live transcription returned nothing; retrying from the recording.")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Live transcription failed ({exc}); using the recording instead.")
            await self._abort_stream()

        configured = self._deps.transcriber()
        if configured is None:
            warnings.append("No transcription provider is configured.")
            return "", False, warnings
        provider, opts = configured
        try:
            result = await provider.transcribe(buffer.to_wav(), opts)
        except Exception as exc:  # noqa: BLE001
            warnings.append(_batch_failure_note(opts.model, exc))
            return "", False, warnings
        return result.text, False, warnings

    async def _transform(self, text: str, settings: Settings) -> TransformOutcome:
        configured = self._deps.transformer()
        provider: TransformProvider | None = None
        options: TransformOptions | None = None
        if configured is not None:
            provider, options = configured
        return await run_steps(
            text,
            settings.transform.steps,
            provider=provider,
            options=options,
            vocabulary=settings.vocabulary,
        )

    # -- teardown -----------------------------------------------------------------------

    async def _cancel_delta_reader(self) -> None:
        task, self._delta_reader = self._delta_reader, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _abort_stream(self) -> None:
        await self._cancel_delta_reader()
        task, self._pump = self._pump, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        session, self._stream_session = self._stream_session, None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.abort()
        self._stream_sink = None

    def _reset(self) -> None:
        if self._tee is not None:
            self._tee.clear()
        self._buffer = None
        self._tee = None
        self._stream_sink = None
        self._stream_session = None
        self._pump = None
        self._partial = ""
        self._set_state(SessionState.IDLE)

    async def shutdown(self) -> None:
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        await self._abort_stream()
        self._reset()


def _batch_failure_note(model: str, exc: Exception) -> str:
    """Explain a failed batch transcription in terms of the cause, not the status code.

    Some models are realtime-only: `gpt-live-transcribe` answers the batch endpoint with a
    bare HTTP 404 "Invalid URL", which read as though yada had the wrong address. When live
    transcription is also unavailable there is nothing left to try, and saying so beats
    reporting a 404 the user cannot act on.
    """
    detail = str(exc)
    if "404" in detail or "Invalid URL" in detail:
        return (
            f"{model} only works with live transcription, and the live connection was not "
            "available, so there was nothing to fall back to."
        )
    return f"Transcription failed: {detail}"
