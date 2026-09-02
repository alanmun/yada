"""The dictation state machine, driven with fake audio and fake providers.

No microphone and no network. The behaviours under test are the ones a user would notice:
the right chimes at the right time, pasting only when asked, and never losing a dictation to
a network failure.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from yada.config import OutputSettings, Settings, TransformStep
from yada.pipeline import session as session_mod
from yada.pipeline.session import DictationSession, SessionDeps, SessionState, Stage
from yada.providers.base import (
    TranscribeOptions,
    TranscriptionCapabilities,
    TranscriptionResult,
    TransformOptions,
    TransformResult,
)

PCM = b"\x01\x02" * 2400  # 0.2s at 24kHz


# --------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------


class FakeCapture:
    """Stands in for PortAudio: feeds fixed frames into the tee on start()."""

    instances: ClassVar[list[FakeCapture]] = []

    def __init__(self, on_frames, *, device=None, gain=1.0, blocksize=1024):
        self.on_frames = on_frames
        self.last_error = None
        self.started = False
        self.frames_to_send = 3
        self.fail = False
        FakeCapture.instances.append(self)

    def start(self):
        if self.fail:
            raise session_mod.AudioDeviceError("no microphone")
        self.started = True
        for _ in range(self.frames_to_send):
            self.on_frames(PCM)

    def stop(self):
        self.started = False


class FakeStream:
    def __init__(self, *, deltas=("hello ", "world"), final="hello world", fail_finish=None):
        self._deltas = list(deltas)
        self._final = final
        self._fail_finish = fail_finish
        self.fed = 0
        self.aborted = False

    async def feed(self, pcm16):
        self.fed += len(pcm16)

    async def deltas(self):
        for d in self._deltas:
            yield d

    async def finish(self):
        if self._fail_finish:
            raise RuntimeError(self._fail_finish)
        return TranscriptionResult(text=self._final, model="live", provider="fake")

    async def abort(self):
        self.aborted = True


class FakeTranscriber:
    id = "fake"
    label = "Fake"

    def __init__(self, *, streaming=False, batch_text="batch text", stream=None,
                 open_fails=None, batch_fails=None):
        self._streaming = streaming
        self._batch_text = batch_text
        self._stream = stream
        self._open_fails = open_fails
        self._batch_fails = batch_fails
        self.batch_calls = 0
        self.last_wav: bytes | None = None

    def capabilities(self):
        return TranscriptionCapabilities(batch=True, streaming=self._streaming, keywords=True)

    async def list_models(self):
        return []

    async def transcribe(self, wav_bytes, opts):
        self.batch_calls += 1
        self.last_wav = wav_bytes
        if self._batch_fails:
            raise RuntimeError(self._batch_fails)
        return TranscriptionResult(text=self._batch_text, model="batch", provider="fake")

    async def open_stream(self, opts):
        if self._open_fails:
            raise RuntimeError(self._open_fails)
        return self._stream or FakeStream()


class FakeTransformer:
    id = "fake"
    label = "Fake"

    def __init__(self, reply="TRANSFORMED", fail=None):
        self.reply, self.fail = reply, fail

    def capabilities(self, model=None):
        from yada.providers.base import TransformCapabilities

        return TransformCapabilities()

    async def list_models(self):
        return []

    async def transform(self, system, user, opts):
        if self.fail:
            raise RuntimeError(self.fail)
        return TransformResult(text=self.reply, model=opts.model, provider="fake")


class Recorder:
    """Captures everything the session emits, in order."""

    def __init__(self):
        self.states: list[SessionState] = []
        self.partials: list[str] = []
        self.finished: list = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.chimes: list[Stage] = []
        self.delivered: list[tuple[str, Stage]] = []

    def on_state(self, state):
        self.states.append(state)

    def on_partial(self, text):
        self.partials.append(text)

    def on_finished(self, result):
        self.finished.append(result)

    def on_error(self, message):
        self.errors.append(message)

    def on_warning(self, message):
        self.warnings.append(message)


@pytest.fixture(autouse=True)
def fake_audio(monkeypatch):
    FakeCapture.instances.clear()
    monkeypatch.setattr(session_mod, "AudioCapture", FakeCapture)
    return FakeCapture


def build(settings=None, transcriber=None, transformer=None):
    settings = settings or Settings()
    rec = Recorder()
    deps = SessionDeps(
        settings=lambda: settings,
        transcriber=(
            (lambda: (transcriber, TranscribeOptions(model="m"))) if transcriber else (lambda: None)
        ),
        transformer=(
            (lambda: (transformer, TransformOptions(model="m"))) if transformer else (lambda: None)
        ),
        events=rec,
        chime=rec.chimes.append,
        deliver=lambda text, stage: rec.delivered.append((text, stage)),
    )
    return DictationSession(asyncio.get_event_loop(), deps), rec, settings


# --------------------------------------------------------------------------------------
# Recording lifecycle
# --------------------------------------------------------------------------------------


async def test_toggle_starts_and_stops(fake_audio):
    sess, rec, _ = build(transcriber=FakeTranscriber())
    await sess.toggle_async()
    assert sess.state is SessionState.RECORDING
    capture = fake_audio.instances[-1]
    assert capture.started is True, "the microphone stream must be open while recording"

    await sess.toggle_async()
    assert sess.state is SessionState.IDLE
    assert capture.started is False, "the microphone must be released on stop"
    assert rec.states == [
        SessionState.RECORDING,
        SessionState.TRANSCRIBING,
        SessionState.IDLE,
    ]
    assert rec.finished[0].transcript == "batch text"


async def test_no_provider_configured_is_reported_not_crashed():
    sess, rec, _ = build(transcriber=None)
    await sess.toggle_async()
    assert sess.state is SessionState.IDLE
    assert "No transcription provider" in rec.errors[0]
    # Tagged so the app can open Settings rather than rely on a tray notification the
    # user may never see -- Windows 11 hides new tray icons behind the overflow arrow.
    assert rec.errors[0].startswith("NOT_CONFIGURED: ")
    assert rec.chimes == [], "nothing was recorded, so nothing should chime"


async def test_microphone_failure_is_reported(fake_audio, monkeypatch):
    sess, rec, _ = build(transcriber=FakeTranscriber())

    def failing_start(self):
        raise session_mod.AudioDeviceError("no microphone")

    monkeypatch.setattr(fake_audio, "start", failing_start)
    await sess.toggle_async()
    assert sess.state is SessionState.IDLE
    assert "no microphone" in rec.errors[0]


async def test_empty_recording_makes_no_api_call(fake_audio):
    stt = FakeTranscriber()
    sess, rec, _ = build(transcriber=stt)
    await sess.toggle_async()
    fake_audio.instances[-1].frames_to_send = 0
    # Drop the audio that was already buffered to simulate a double-tap of the hotkey.
    sess._buffer.clear()
    await sess.toggle_async()
    assert stt.batch_calls == 0, "a zero-length recording must not be uploaded"
    assert rec.chimes == [Stage.LISTENING], "it did start listening, so that chime is right"
    assert "Nothing was recorded" in rec.warnings[0]
    assert sess.state is SessionState.IDLE


async def test_toggle_while_processing_warns(fake_audio):
    sess, rec, _ = build(transcriber=FakeTranscriber())
    await sess.toggle_async()
    sess._state = SessionState.TRANSCRIBING
    await sess.toggle_async()
    assert any("Still finishing" in w for w in rec.warnings)


# --------------------------------------------------------------------------------------
# Streaming vs batch
# --------------------------------------------------------------------------------------


async def test_streaming_provider_streams_and_surfaces_partials(fake_audio):
    stream = FakeStream(deltas=("hel", "lo"), final="hello")
    stt = FakeTranscriber(streaming=True, stream=stream)
    sess, rec, _ = build(transcriber=stt)
    await sess.toggle_async()
    await asyncio.sleep(0.05)  # let the delta reader run
    await sess.toggle_async()

    assert rec.finished[0].transcript == "hello"
    assert rec.finished[0].streamed is True
    assert stt.batch_calls == 0, "streaming success must not also upload the buffer"
    assert rec.partials == ["hel", "hello"], "partials accumulate for live display"
    assert stream.fed > 0, "audio must actually reach the socket"


async def test_stream_open_failure_falls_back_to_batch(fake_audio):
    stt = FakeTranscriber(streaming=True, open_fails="handshake refused", batch_text="from buffer")
    sess, rec, _ = build(transcriber=stt)
    await sess.toggle_async()
    await sess.toggle_async()

    assert rec.finished[0].transcript == "from buffer"
    assert rec.finished[0].streamed is False
    assert stt.batch_calls == 1
    assert any("Live transcription unavailable" in w for w in rec.warnings)


async def test_stream_finish_failure_falls_back_to_batch(fake_audio):
    stream = FakeStream(fail_finish="socket closed")
    stt = FakeTranscriber(streaming=True, stream=stream, batch_text="rescued")
    sess, rec, _ = build(transcriber=stt)
    await sess.toggle_async()
    await sess.toggle_async()

    assert rec.finished[0].transcript == "rescued", (
        "a dropped socket must not lose the dictation"
    )
    assert stt.batch_calls == 1
    assert any("socket closed" in w for w in rec.finished[0].warnings)


async def test_empty_stream_result_falls_back_to_batch(fake_audio):
    stream = FakeStream(deltas=(), final="")
    stt = FakeTranscriber(streaming=True, stream=stream, batch_text="second try")
    sess, rec, _ = build(transcriber=stt)
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.finished[0].transcript == "second try"


async def test_streaming_disabled_by_setting(fake_audio):
    settings = Settings()
    settings.transcription.prefer_streaming = False
    stt = FakeTranscriber(streaming=True, batch_text="batched")
    sess, rec, _ = build(settings=settings, transcriber=stt)
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.finished[0].streamed is False
    assert stt.batch_calls == 1


async def test_batch_failure_is_reported(fake_audio):
    stt = FakeTranscriber(batch_fails="401 unauthorised")
    sess, rec, _ = build(transcriber=stt)
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.errors and "no text" in rec.errors[0]
    assert sess.state is SessionState.IDLE


async def test_wav_payload_is_a_real_wav(fake_audio):
    stt = FakeTranscriber()
    sess, _, _ = build(transcriber=stt)
    await sess.toggle_async()
    await sess.toggle_async()
    assert stt.last_wav is not None
    assert stt.last_wav[:4] == b"RIFF" and stt.last_wav[8:12] == b"WAVE"


# --------------------------------------------------------------------------------------
# Chimes, transform and delivery
# --------------------------------------------------------------------------------------


async def test_listening_chime_fires_the_moment_recording_starts(fake_audio):
    """The only confirmation the shortcut fired at all.

    Without it, pressing the shortcut produced no feedback until transcription finished --
    and none ever if anything was misconfigured, which is indistinguishable from a
    shortcut that never registered.
    """
    sess, rec, _ = build(transcriber=FakeTranscriber())
    await sess.toggle_async()
    assert rec.chimes == [Stage.LISTENING], "must chime on start, before anything else"


async def test_two_chimes_when_no_transform(fake_audio):
    sess, rec, _ = build(transcriber=FakeTranscriber())
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.chimes == [Stage.LISTENING, Stage.TRANSCRIPTION]


async def test_three_chimes_when_transform_runs(fake_audio):
    settings = Settings()
    settings.transform.enabled = True
    settings.transform.steps = [TransformStep(type="prompt_transform")]
    sess, rec, _ = build(
        settings=settings, transcriber=FakeTranscriber(), transformer=FakeTransformer()
    )
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.chimes == [Stage.LISTENING, Stage.TRANSCRIPTION, Stage.TRANSFORMATION]
    assert rec.finished[0].final_text == "TRANSFORMED"
    assert rec.finished[0].transcript == "batch text"


async def test_transform_failure_still_yields_transcript(fake_audio):
    settings = Settings()
    settings.transform.enabled = True
    settings.transform.steps = [TransformStep(type="prompt_transform")]
    sess, rec, _ = build(
        settings=settings,
        transcriber=FakeTranscriber(),
        transformer=FakeTransformer(fail="500 server error"),
    )
    await sess.toggle_async()
    await sess.toggle_async()
    result = rec.finished[0]
    assert result.final_text == "batch text", "a failed cleanup must not lose the words"
    assert any("500 server error" in w for w in result.warnings)
    # The transform chime still fires: the stage completed, just unsuccessfully.
    assert rec.chimes == [Stage.LISTENING, Stage.TRANSCRIPTION, Stage.TRANSFORMATION]


async def test_no_paste_by_default(fake_audio):
    sess, rec, _ = build(transcriber=FakeTranscriber())
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.delivered == [], "auto-paste must be opt-in"


async def test_paste_after_transcription(fake_audio):
    settings = Settings(output=OutputSettings(paste_mode="after_transcription"))
    sess, rec, _ = build(settings=settings, transcriber=FakeTranscriber())
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.delivered == [("batch text", Stage.TRANSCRIPTION)]


async def test_paste_after_transformation(fake_audio):
    settings = Settings(output=OutputSettings(paste_mode="after_transformation"))
    settings.transform.enabled = True
    settings.transform.steps = [TransformStep(type="prompt_transform")]
    sess, rec, _ = build(
        settings=settings, transcriber=FakeTranscriber(), transformer=FakeTransformer()
    )
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.delivered == [("TRANSFORMED", Stage.TRANSFORMATION)]


async def test_paste_after_transformation_with_no_transform_still_pastes(fake_audio):
    """Otherwise this combination of settings would mean nothing is ever pasted."""
    settings = Settings(output=OutputSettings(paste_mode="after_transformation"))
    settings.transform.enabled = False
    sess, rec, _ = build(settings=settings, transcriber=FakeTranscriber())
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.delivered == [("batch text", Stage.TRANSCRIPTION)]
    assert any("No transform is configured" in w for w in rec.finished[0].warnings)


async def test_shutdown_is_safe_mid_recording(fake_audio):
    stt = FakeTranscriber(streaming=True)
    sess, _, _ = build(transcriber=stt)
    await sess.toggle_async()
    await sess.shutdown()
    assert sess.state is SessionState.IDLE


# --------------------------------------------------------------------------------------
# Nothing the user can perceive waits for the network
# --------------------------------------------------------------------------------------


class SlowOpenTranscriber(FakeTranscriber):
    """A provider whose socket takes as long to open as the caller allows."""

    def __init__(self, **kwargs):
        super().__init__(streaming=True, **kwargs)
        self.released = asyncio.Event()
        self.open_started = asyncio.Event()

    async def open_stream(self, opts):
        self.open_started.set()
        await self.released.wait()
        return self._stream or FakeStream()


async def test_the_listening_chime_does_not_wait_for_the_socket(fake_audio):
    """The reported bug: about three seconds before anything happened.

    Opening the socket -- DNS, TLS, handshake, a `session.updated` round trip, measured at
    0.3-1.3s and sometimes worse -- sat in front of the state change and the chime. The
    chime is the only confirmation the shortcut fired at all, so it has to come first.
    """
    stream = FakeStream()
    stt = SlowOpenTranscriber(stream=stream)
    sess, rec, _ = build(transcriber=stt)

    await sess.toggle_async()

    assert sess.state is SessionState.RECORDING, "recording must start before the socket"
    assert rec.chimes == [Stage.LISTENING], "the chime must not wait on the network"
    # Not even started: the connect is a task that has not been given a turn yet, so it
    # contributes nothing at all to the time between the keypress and the chime.
    assert not stt.open_started.is_set()
    await asyncio.sleep(0)
    assert stt.open_started.is_set(), "the connect should then proceed on its own"

    stt.released.set()
    await sess.toggle_async()
    assert rec.finished[0].transcript == "hello world"


async def test_audio_spoken_during_the_connect_still_reaches_the_stream(fake_audio):
    """It used to not even be recorded.

    The microphone was opened *after* the socket, so anything said while connecting was
    gone -- not merely missing from the live transcript. The sink is attached first now and
    its queue is drained once the pump starts, so the stream still gets the first words.
    """
    stream = FakeStream()
    stt = SlowOpenTranscriber(stream=stream)
    sess, rec, _ = build(transcriber=stt)

    await sess.toggle_async()
    capture = fake_audio.instances[-1]
    assert capture.started, "the microphone is open while the socket is still connecting"
    fed_before = stream.fed
    assert fed_before == 0, "nothing can have been sent yet -- there is no socket"

    # More speech, still mid-connect.
    capture.on_frames(PCM)
    stt.released.set()
    await sess.toggle_async()

    assert stream.fed > 0, "the queued audio must be sent once the socket arrives"
    assert rec.finished[0].streamed is True


async def test_a_recording_shorter_than_the_connect_still_streams(fake_audio):
    """Stopping before the socket is open must not silently mean batch.

    For a realtime-only model such as gpt-live-transcribe batch is an HTTP 404, so
    abandoning a half-open connect would produce no transcript at all.
    """
    stream = FakeStream()
    stt = SlowOpenTranscriber(stream=stream)
    sess, rec, _ = build(transcriber=stt)

    await sess.toggle_async()
    stop = asyncio.create_task(sess.toggle_async())
    await asyncio.sleep(0)  # let the stop reach the point where it waits
    stt.released.set()
    await stop

    assert rec.finished[0].streamed is True, "the stop should wait briefly for the socket"
    assert stt.batch_calls == 0, "batch is not a fallback for a realtime-only model"


async def test_a_connect_that_never_arrives_does_not_hang_the_stop(fake_audio, monkeypatch):
    """A dead network must cost the grace period, not the dictation."""
    monkeypatch.setattr(session_mod, "STREAM_CONNECT_GRACE", 0.05)
    stt = SlowOpenTranscriber(stream=FakeStream(), batch_text="rescued")
    sess, rec, _ = build(transcriber=stt)

    await sess.toggle_async()
    await sess.toggle_async()  # never released

    assert rec.finished[0].transcript == "rescued"
    assert stt.batch_calls == 1
    assert any("did not finish opening" in w for w in rec.finished[0].warnings)


async def test_a_failed_connect_stops_queueing_audio(fake_audio):
    """A sink left on the tee fills up and reports phantom dropped audio.

    The drop count is shown to the user as lost audio, so a stream that never existed must
    not produce one.
    """
    stt = FakeTranscriber(streaming=True, open_fails="no route to host", batch_text="ok")
    sess, rec, _ = build(transcriber=stt)

    await sess.toggle_async()
    await asyncio.sleep(0)  # let the connect task fail
    capture = fake_audio.instances[-1]
    for _ in range(400):  # far more than the sink's queue would hold
        capture.on_frames(PCM)
    await sess.toggle_async()

    assert rec.finished[0].transcript == "ok"
    assert not any("dropped" in w for w in rec.finished[0].warnings), (
        "a stream that never opened must not report dropped audio"
    )
