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
    TranscriptionResult,
    TransformOptions,
    TransformProvider,
)
from .recordings import Recording, RecordingStore, new_recording_id, now_iso
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
    # Where finished recordings go, so a failed transcription can be retried instead of
    # repeated. None disables keeping any.
    recordings: RecordingStore | None = None


# How long a stop will wait for a socket that is still opening. Long enough to cover a slow
# connect, short enough that a dead network does not hold the transcript hostage.
STREAM_CONNECT_GRACE = 3.0

# How long to let queued audio finish uploading before committing. A backlog builds while
# the socket opens, so this covers sending it rather than just the last chunk.
UPLOAD_DRAIN_TIMEOUT = 30.0


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
        # Opening the socket happens off the start path, so it is a task that may still be
        # in flight when the user stops talking. See `_start`.
        self._stream_connect: asyncio.Task | None = None
        self._partial = ""
        self._transcription_result: TranscriptionResult | None = None
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
            try:
                await self._stop_and_process()
            except Exception as exc:  # noqa: BLE001 - the app must stay usable regardless
                # Without this the session stays on TRANSCRIBING for good: every later
                # keypress is answered with "Still finishing the last dictation…" and the
                # only way out is to restart yada. One lost dictation is the right price.
                await self._abort_stream()
                self._reset()
                self._deps.events.on_error(f"Dictation failed: {exc}")
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

        # Streaming is set up in two halves, and the order matters more than it looks.
        #
        # Opening the socket used to happen here, before the microphone: DNS, TLS, the
        # websocket handshake and a `session.updated` round trip, measured at 0.3-1.3s and
        # occasionally worse. Everything the user can perceive -- the state change and the
        # listening chime -- waited behind it, so pressing the shortcut appeared to do
        # nothing for up to three seconds. Worse than the lag: the microphone was not open
        # yet, so anything said in that window was not merely missing from the live
        # transcript, it was never recorded at all.
        #
        # So the sink is attached now and the socket is opened afterwards, in the
        # background. The sink queues about twenty seconds of audio, which is ample cover
        # for a connect, and the queued chunks are sent the moment the pump starts -- so
        # the live transcript still begins at the first word.
        streaming = settings.transcription.prefer_streaming and provider.capabilities().streaming
        if streaming:
            sink = StreamSink(self._loop)
            self._stream_sink = sink
            self._tee.add(sink)

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
        # "tried to listen". Nothing slower than that is allowed in front of it.
        self._deps.chime(Stage.LISTENING)

        if streaming:
            self._stream_connect = asyncio.create_task(self._open_stream(provider, opts))

    async def _open_stream(self, provider: TranscriptionProvider, opts: TranscribeOptions) -> None:
        """Open the live socket while recording is already under way.

        Runs as a task so nothing the user can hear or see waits for the network. Audio has
        been queueing in the sink since the microphone opened, and starting the pump drains
        it in order, so the transcript still starts at the beginning of the recording.
        """
        sink = self._stream_sink
        try:
            session = await provider.open_stream(opts)
        except Exception as exc:  # noqa: BLE001 - any failure just means batch instead
            self._deps.events.on_warning(
                f"Live transcription unavailable, will transcribe on stop ({exc})"
            )
            self._detach_stream_sink(sink)
            return
        if self._tee is None or sink is not self._stream_sink:
            # The recording was torn down or replaced while the socket was opening, so
            # there is nothing to attach it to, and leaving it open would hold a paid
            # session for a recording that no longer exists.
            #
            # Deliberately *not* keyed on the state being RECORDING: a recording shorter
            # than the connect ends with the state already moved on to TRANSCRIBING, and
            # that session is exactly the one the stop path is waiting for. Checking the
            # state here threw it away and fell back to batch on every short dictation.
            with contextlib.suppress(Exception):
                await session.abort()
            return
        self._stream_session = session
        self._pump = asyncio.create_task(sink.pump(session.feed))
        self._delta_reader = asyncio.create_task(self._read_deltas(session))

    def _detach_stream_sink(self, sink: StreamSink | None) -> None:
        """Stop queueing audio nobody is going to send.

        Without this a failed connect leaves the sink on the tee, filling its queue for the
        rest of the recording and counting every chunk past the cap as dropped -- which
        then gets reported to the user as lost audio, from a stream that never existed.
        """
        if sink is None:
            return
        if self._tee is not None:
            self._tee.remove(sink)
        if sink is self._stream_sink:
            self._stream_sink = None

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

            # Stored before anything can go wrong with the *rest* of the pipeline, and
            # whether or not the transcription worked. A network error arrives after you
            # have finished speaking, so the audio is always complete by this point --
            # discarding it was throwing away a recording that was already good.
            await self._remember(buffer, duration, transcript, streamed, warnings)

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

    async def _remember(
        self,
        buffer: WavBuffer,
        duration: float,
        transcript: str,
        streamed: bool,
        warnings: list[str],
    ) -> None:
        """Write the recording and its outcome to the store. Never fatal."""
        store = self._deps.recordings
        if store is None or store.keep <= 0:
            return
        configured = self._deps.transcriber()
        model = configured[1].model if configured else ""
        provider = getattr(configured[0], "id", "") if configured else ""
        if self._transcription_result is not None:
            model = self._transcription_result.model
            provider = self._transcription_result.provider
        entry = Recording(
            id=new_recording_id(),
            recorded_at=now_iso(),
            duration_seconds=round(duration, 2),
            transcript=transcript,
            error="" if transcript else " ".join(warnings).strip(),
            model=model,
            provider=provider,
            streamed=streamed,
        )
        with contextlib.suppress(Exception):
            wav = await asyncio.to_thread(buffer.to_wav)
            await asyncio.to_thread(store.add, wav, entry)

    async def retry_last(self, *, deliver: bool = True) -> str:
        """Transcribe the most recent recording again. What holding the shortcut does."""
        return await self.retry(None, deliver=deliver)

    async def retry(self, recording_id: str | None = None, *, deliver: bool = True) -> str:
        """Transcribe a stored recording again. Returns the transcript, or "".

        The point of the whole feature: a transcription failure is a failed *request*
        against audio that was already complete, so it is worth another try rather than
        asking the user to say it all again.

        `recording_id` of None means the most recent. `deliver=False` is for the Recordings
        tab, where pasting would land in yada's own window rather than wherever the user
        actually wants the text.
        """
        store = self._deps.recordings
        if store is None or store.keep <= 0:
            self._deps.events.on_error(
                "Recordings are not being kept, so there is nothing to retry."
            )
            return ""
        if self._state is not SessionState.IDLE:
            self._deps.events.on_warning("Still finishing the last dictation…")
            return ""
        if self._busy.locked():
            return ""

        async with self._busy:
            if recording_id is None:
                latest = await asyncio.to_thread(store.latest)
            else:
                latest = next(
                    (r for r in await asyncio.to_thread(store.entries) if r.id == recording_id),
                    None,
                )
            if latest is None:
                self._deps.events.on_error("There is no recent recording to retry.")
                return ""
            wav = await asyncio.to_thread(store.read_audio, latest.id)
            if not wav:
                self._deps.events.on_error("That recording's audio is missing.")
                return ""

            configured = self._deps.transcriber()
            if configured is None:
                self._deps.events.on_error(
                    "NOT_CONFIGURED: No transcription provider is configured yet. "
                    "Add an API key on the Providers tab."
                )
                return ""
            provider, opts = configured

            self._set_state(SessionState.TRANSCRIBING)
            try:
                result = await provider.transcribe(wav, opts)
            except Exception as exc:  # noqa: BLE001 - a retry that fails must not wedge anything
                self._set_state(SessionState.IDLE)
                self._deps.events.on_error(f"Retry failed: {_batch_failure_note(opts.model, exc)}")
                return ""

            text = (result.text or "").strip()
            if not text:
                self._set_state(SessionState.IDLE)
                self._deps.events.on_error("The retry produced no text.")
                return ""

            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    store.update,
                    latest.id,
                    transcript=text,
                    error="",
                    model=result.model,
                    provider=result.provider,
                    streamed=False,
                )

            self._deps.chime(Stage.TRANSCRIPTION)
            settings = self._deps.settings()
            if deliver and settings.output.paste_mode != "off":
                self._deps.deliver(text, Stage.TRANSCRIPTION)
            self._set_state(SessionState.IDLE)
            self._deps.events.on_finished(
                SessionResult(
                    transcript=text,
                    final_text=text,
                    duration_seconds=latest.duration_seconds,
                    streamed=False,
                    transform=None,
                    warnings=["Retried the previous recording."],
                )
            )
            return text

    async def _transcribe(self, buffer: WavBuffer) -> tuple[str, bool, list[str]]:
        """Prefer the already-open stream; fall back to batch on any failure."""
        warnings: list[str] = []
        await self._settle_stream_connect(warnings)
        if self._stream_sink is not None and self._stream_session is not None:
            self._stream_sink.flush()
            if self._pump is not None:
                # Committing before the queued audio has finished uploading transcribes
                # only what arrived, which reads as the model mishearing rather than as
                # missing audio -- so a timeout here says so instead of passing silently.
                try:
                    await asyncio.wait_for(self._pump, timeout=UPLOAD_DRAIN_TIMEOUT)
                except TimeoutError:
                    warnings.append(
                        "Some audio had not finished uploading, so the transcript may be cut short."
                    )
                except Exception:  # noqa: BLE001 - a dead pump is handled by finish()
                    pass
            if self._stream_sink.dropped_chunks:
                warnings.append(
                    f"{self._stream_sink.dropped_chunks} audio chunks were dropped from the "
                    "live stream; the saved recording is still complete."
                )
            try:
                result = await self._stream_session.finish()
                await self._cancel_delta_reader()
                if result.text:
                    self._transcription_result = result
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
        self._transcription_result = result
        if result.model != opts.model:
            warnings.append(
                f"The recording was transcribed with {result.model} instead of {opts.model}."
            )
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

    async def _settle_stream_connect(self, warnings: list[str]) -> None:
        """Give a socket that is still opening a moment to arrive before choosing a path.

        A recording can easily be shorter than a connect. Abandoning it the instant the
        user stops talking would mean uploading the audio again instead of using the
        live connection. If this grace expires, the provider must choose a model that
        accepts files for the fallback.
        """
        task, self._stream_connect = self._stream_connect, None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=STREAM_CONNECT_GRACE)
        except (TimeoutError, asyncio.CancelledError):
            # wait_for has already cancelled it.
            warnings.append("The live connection did not finish opening in time.")
        except Exception:  # noqa: BLE001 - it reports its own failure as a warning
            return

    async def _cancel_stream_connect(self) -> None:
        task, self._stream_connect = self._stream_connect, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _abort_stream(self) -> None:
        await self._cancel_stream_connect()
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
        task, self._stream_connect = self._stream_connect, None
        if task is not None:
            task.cancel()
        self._buffer = None
        self._tee = None
        self._stream_sink = None
        self._stream_session = None
        self._pump = None
        self._partial = ""
        self._transcription_result = None
        self._set_state(SessionState.IDLE)

    async def shutdown(self) -> None:
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        await self._abort_stream()
        self._reset()


def _batch_failure_note(model: str, exc: Exception) -> str:
    """Keep the actual failure; a 404 alone cannot identify a model's capabilities."""
    return f"Transcription failed (selected model: {model}): {exc}"
