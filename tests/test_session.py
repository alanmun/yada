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
    pcm: ClassVar[bytes] = PCM
    substitution: ClassVar[str | None] = None

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
            self.on_frames(self.pcm)

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
        self.discarded = 0
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

    def on_discarded(self):
        self.discarded += 1


@pytest.fixture(autouse=True)
def fake_audio(monkeypatch):
    FakeCapture.instances.clear()
    monkeypatch.setattr(session_mod, "AudioCapture", FakeCapture)
    return FakeCapture


def build(settings=None, transcriber=None, transformer=None, recordings=None):
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
        recordings=recordings,
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
    sess._active._buffer.clear()
    await sess.toggle_async()
    assert stt.batch_calls == 0, "a zero-length recording must not be uploaded"
    assert rec.chimes == [Stage.LISTENING], "it did start listening, so that chime is right"
    assert rec.warnings == [] and rec.discarded == 1, "a double-tap is a cancel, not a fault"
    assert sess.state is SessionState.IDLE


async def test_a_long_recording_with_no_audio_says_so(fake_audio, monkeypatch):
    monkeypatch.setattr(session_mod, "MISPRESS_SECONDS", 0.0)
    sess, rec, _ = build(transcriber=FakeTranscriber())
    await sess.toggle_async()
    fake_audio.instances[-1].frames_to_send = 0
    sess._active._buffer.clear()
    await sess.toggle_async()
    assert "Nothing was recorded" in rec.warnings[0]


async def test_a_mispress_that_hears_nothing_ends_quietly(fake_audio):
    sess, rec, _ = build(transcriber=FakeTranscriber(batch_text=""))
    await sess.toggle_async()
    await sess.toggle_async()
    for _ in range(50):
        if rec.discarded or rec.errors:
            break
        await asyncio.sleep(0.01)
    assert rec.errors == [], "no 'produced no text' for a press-and-cancel"
    assert rec.discarded == 1
    assert sess.state is SessionState.IDLE


async def test_a_longer_recording_that_hears_nothing_is_still_reported(fake_audio, monkeypatch):
    monkeypatch.setattr(session_mod, "MISPRESS_SECONDS", 0.0)
    sess, rec, _ = build(transcriber=FakeTranscriber(batch_text=""))
    await sess.toggle_async()
    await sess.toggle_async()
    for _ in range(50):
        if rec.discarded or rec.errors:
            break
        await asyncio.sleep(0.01)
    assert rec.discarded == 0
    assert "produced no text" in rec.errors[0]


async def test_a_silent_recording_is_never_sent_for_transcription(fake_audio, monkeypatch):
    """A dead input plus a vocabulary prompt made the model answer with the vocabulary."""
    monkeypatch.setattr(session_mod, "MISPRESS_SECONDS", 0.0)
    monkeypatch.setattr(fake_audio, "pcm", b"\x10\x00" * 2400)  # peak 16/32767, about -66 dB
    stream = FakeStream(deltas=(), final="Vocabulary. Echo.")
    stt = FakeTranscriber(streaming=True, stream=stream, batch_text="Vocabulary. Echo.")
    sess, rec, _ = build(transcriber=stt, transformer=FakeTransformer())
    await sess.toggle_async()
    await sess.toggle_async()
    assert stt.batch_calls == 0, "silence must not be uploaded"
    assert rec.delivered == [] and rec.finished == [], "nothing may be pasted or copied"
    assert "only silence" in rec.errors[0]
    assert sess.state is SessionState.IDLE


async def test_a_missing_microphone_is_said_out_loud(fake_audio, monkeypatch):
    monkeypatch.setattr(fake_audio, "substitution", "Your microphone (X) is not connected.")
    sess, rec, _ = build(transcriber=FakeTranscriber())
    await sess.toggle_async()
    await sess.toggle_async()
    assert any("not connected" in w for w in rec.finished[0].warnings)


async def test_a_short_recording_whose_request_fails_is_still_reported(fake_audio):
    sess, rec, _ = build(transcriber=FakeTranscriber(batch_fails="401 bad key"))
    await sess.toggle_async()
    await sess.toggle_async()
    for _ in range(50):
        if rec.discarded or rec.errors:
            break
        await asyncio.sleep(0.01)
    assert rec.discarded == 0
    assert "401 bad key" in rec.errors[0]




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


async def test_pastes_the_transcript_by_default(fake_audio):
    sess, rec, _ = build(transcriber=FakeTranscriber())
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.delivered == [("batch text", Stage.TRANSCRIPTION)], (
        "pasting is the point of dictation; a fresh install should not need a settings trip"
    )


async def test_off_pastes_nothing(fake_audio):
    settings = Settings(output=OutputSettings(paste_mode="off"))
    sess, rec, _ = build(settings=settings, transcriber=FakeTranscriber())
    await sess.toggle_async()
    await sess.toggle_async()
    assert rec.delivered == []


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

    Give an almost-ready stream a chance before uploading the recording again.
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
    assert stt.batch_calls == 0, "an almost-ready stream should avoid another upload"


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


async def test_a_failure_while_delivering_does_not_wedge_the_session(fake_audio):
    """Any failure in the stop path has to return the app to a usable state.

    A hang in delivery -- Qt's clipboard called from the asyncio thread, which is what
    happened -- left the session on TRANSCRIBING permanently. Every later keypress was
    answered with "Still finishing the last dictation…" and restarting yada was the only
    way out. One lost dictation is the right price for a bug here.
    """
    settings = Settings()
    settings.output.paste_mode = "after_transcription"
    sess, rec, _ = build(settings=settings, transcriber=FakeTranscriber(batch_text="hello"))

    def explode(_text, _stage):
        raise RuntimeError("clipboard is wedged")

    sess._deps = type(sess._deps)(
        settings=sess._deps.settings,
        transcriber=sess._deps.transcriber,
        transformer=sess._deps.transformer,
        events=rec,
        chime=rec.chimes.append,
        deliver=explode,
    )

    await sess.toggle_async()
    await sess.toggle_async()

    assert sess.state is SessionState.IDLE, "the session must recover, not stay busy"
    assert any("clipboard is wedged" in e for e in rec.errors)

    # And it can be used again immediately.
    await sess.toggle_async()
    assert sess.state is SessionState.RECORDING


# --------------------------------------------------------------------------------------
# Keeping the recording, so a failed transcription is not a lost dictation
# --------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    from yada.pipeline.recordings import RecordingStore

    return RecordingStore(directory=tmp_path / "recordings", keep=5)


async def test_a_successful_dictation_is_kept(fake_audio, store):
    stt = FakeTranscriber(batch_text="hello there")
    sess, _rec, _ = build(transcriber=stt, recordings=store)
    await sess.toggle_async()
    await sess.toggle_async()

    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].transcript == "hello there"
    assert entries[0].succeeded is True
    assert entries[0].model == "batch", "record the actual result model, including fallbacks"
    assert entries[0].provider == "fake"
    assert store.read_audio(entries[0].id), "the audio has to be there to retry"


async def test_a_failed_transcription_still_keeps_the_audio(fake_audio, store):
    """The whole point. The recording was complete; only the request failed."""
    stt = FakeTranscriber(batch_fails="connection reset")
    sess, rec, _ = build(transcriber=stt, recordings=store)
    await sess.toggle_async()
    await sess.toggle_async()

    assert rec.errors, "the user is still told it failed"
    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].transcript == ""
    assert "connection reset" in entries[0].error
    assert store.read_audio(entries[0].id), "and the audio survives for a retry"


async def test_keeping_none_stores_nothing(fake_audio, tmp_path):
    from yada.pipeline.recordings import RecordingStore

    off = RecordingStore(directory=tmp_path / "recordings", keep=0)
    sess, _rec, _ = build(transcriber=FakeTranscriber(), recordings=off)
    await sess.toggle_async()
    await sess.toggle_async()
    assert off.entries() == [], "0 must mean no audio is written at all"


async def test_retry_transcribes_the_last_recording_again(fake_audio, store):
    """A network error the first time, and the retry succeeds without speaking again."""
    stt = FakeTranscriber(batch_fails="network unreachable")
    sess, rec, settings = build(transcriber=stt, recordings=store)
    settings.output.paste_mode = "after_transcription"
    await sess.toggle_async()
    await sess.toggle_async()
    assert store.entries()[0].transcript == ""

    # The network comes back.
    working = FakeTranscriber(batch_text="what I said earlier")
    sess._deps = type(sess._deps)(
        settings=sess._deps.settings,
        transcriber=lambda: (working, TranscribeOptions(model="m")),
        transformer=sess._deps.transformer,
        events=rec,
        chime=rec.chimes.append,
        deliver=lambda text, stage: rec.delivered.append((text, stage)),
        recordings=store,
    )

    text = await sess.retry_last()
    assert text == "what I said earlier"
    assert working.batch_calls == 1
    assert working.last_wav, "it must send the stored audio, not a fresh recording"
    assert store.entries()[0].transcript == "what I said earlier", "the outcome is updated"
    assert rec.delivered[-1][0] == "what I said earlier"
    assert Stage.TRANSCRIPTION in rec.chimes
    assert sess.state is SessionState.IDLE


async def test_retry_does_not_record_anything_new(fake_audio, store):
    """Holding the shortcut must not start listening -- that is the point of holding it."""
    stt = FakeTranscriber(batch_text="first")
    sess, _rec, _ = build(transcriber=stt, recordings=store)
    await sess.toggle_async()
    await sess.toggle_async()
    before = len(fake_audio.instances)

    await sess.retry_last()
    assert len(fake_audio.instances) == before, "no microphone should be opened"
    assert len(store.entries()) == 1, "and no second recording should appear"


async def test_retry_with_nothing_recorded_says_so(fake_audio, store):
    sess, rec, _ = build(transcriber=FakeTranscriber(), recordings=store)
    assert await sess.retry_last() == ""
    assert any("no recent recording" in e for e in rec.errors)


async def test_retry_is_refused_while_recording(fake_audio, store):
    sess, rec, _ = build(transcriber=FakeTranscriber(), recordings=store)
    await sess.toggle_async()
    assert sess.state is SessionState.RECORDING

    assert await sess.retry_last() == ""
    assert any("Still finishing" in w for w in rec.warnings)
    assert sess.state is SessionState.RECORDING, "and the recording is undisturbed"


async def test_a_failing_retry_leaves_the_recording_alone(fake_audio, store):
    """So it can be retried again, rather than being marked off after one more failure."""
    stt = FakeTranscriber(batch_fails="still down")
    sess, rec, _ = build(transcriber=stt, recordings=store)
    await sess.toggle_async()
    await sess.toggle_async()

    assert await sess.retry_last() == ""
    assert any("Retry failed" in e for e in rec.errors)
    assert store.read_audio(store.entries()[0].id), "the audio is still there to try again"
    assert sess.state is SessionState.IDLE


async def test_openai_live_timeout_recovers_with_file_model(monkeypatch, store):
    """The Windows failure: live connect stalls, then the live model rejects file upload."""
    import httpx

    from yada.providers.openai_provider import OpenAITranscription

    monkeypatch.setattr(session_mod, "STREAM_CONNECT_GRACE", 0.01)
    provider = OpenAITranscription("test")
    requests = []

    async def stalled_connect(opts):
        await asyncio.Future()

    def respond(request):
        requests.append(request)
        if b'gpt-live-transcribe' in request.content:
            return httpx.Response(400, json={"error": {
                "message": "Invalid request.", "type": "invalid_request_error",
                "param": None, "code": "invalid_parameter",
            }})
        assert b'gpt-transcribe' in request.content
        assert b'RIFF' in request.content
        return httpx.Response(200, json={"text": "Recovered the recording"})

    monkeypatch.setattr(provider, "open_stream", stalled_connect)
    monkeypatch.setattr(provider, "_client", lambda: httpx.AsyncClient(
        base_url="https://api.openai.com/v1", transport=httpx.MockTransport(respond),
    ))
    sess, rec, _ = build(transcriber=provider, recordings=store)
    sess._deps.transcriber = lambda: (provider, TranscribeOptions(model="gpt-live-transcribe"))
    await sess.toggle_async()
    await sess.toggle_async()

    assert not rec.errors
    assert rec.finished[0].transcript == "Recovered the recording"
    assert not rec.finished[0].streamed
    assert len(requests) == 1
    assert store.entries()[0].model == "gpt-transcribe"
    assert any("did not finish opening" in w for w in rec.finished[0].warnings)
    assert await sess.retry_last(deliver=False) == "Recovered the recording"
    assert store.entries()[0].model == "gpt-transcribe"


class GatedTranscriber(FakeTranscriber):
    """Two requests that tests can finish in either order, without timing guesses."""

    def __init__(self):
        super().__init__()
        self.started = [asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event()]

    async def transcribe(self, wav_bytes, opts):
        index = self.batch_calls
        self.batch_calls += 1
        self.started[index].set()
        await self.release[index].wait()
        return TranscriptionResult(text=("first", "second")[index], model="m", provider="fake")


@pytest.mark.parametrize("paste_mode", ["off", "after_transcription", "after_transformation"])
async def test_new_recording_is_immediate_and_results_keep_speech_order(paste_mode, store):
    stt = GatedTranscriber()
    settings = Settings(output=OutputSettings(paste_mode=paste_mode))
    sess, rec, _ = build(settings=settings, transcriber=stt, recordings=store)
    await sess.toggle_async()
    first_stop = asyncio.create_task(sess.toggle_async())
    await asyncio.wait_for(stt.started[0].wait(), 1)
    assert not FakeCapture.instances[0].started

    # No extra confirmation or release of the preceding request is needed to listen.
    await asyncio.wait_for(sess.toggle_async(), 1)
    assert sess.state is SessionState.RECORDING
    assert FakeCapture.instances[1].started
    assert rec.chimes.count(Stage.LISTENING) == 2
    second_stop = asyncio.create_task(sess.toggle_async())
    await asyncio.wait_for(stt.started[1].wait(), 1)

    stt.release[1].set()
    # The second transcript is saved, proving it processed while the first was blocked.
    # Wait for the transcript, not just the entry: the recording is stored before its
    # transcript is filled in, and a slow Windows runner caught that gap.
    for _ in range(2000):
        if store.entries() and store.entries()[0].transcript:
            break
        await asyncio.sleep(0.001)
    assert store.entries()[0].transcript == "second"
    assert rec.finished == []
    assert rec.delivered == []

    stt.release[0].set()
    await asyncio.wait_for(asyncio.gather(first_stop, second_stop), 2)
    assert [r.transcript for r in rec.finished] == ["first", "second"]
    assert [r.transcript for r in store.entries()] == ["second", "first"]
    expected = [] if paste_mode == "off" else ["first", "second"]
    assert [text for text, _ in rec.delivered] == expected
    assert sess.state is SessionState.IDLE
    assert rec.errors == []


async def test_cleanup_completion_does_not_stop_the_next_recording():
    class GatedTransformer(FakeTransformer):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def transform(self, system, user, opts):
            self.started.set()
            await self.release.wait()
            return await super().transform(system, user, opts)

    transform = GatedTransformer()
    settings = Settings()
    settings.transform.enabled = True
    settings.transform.steps = [TransformStep(type="prompt_transform")]
    sess, rec, _ = build(
        settings=settings, transcriber=FakeTranscriber(), transformer=transform,
    )
    pending_counts = []
    sess._deps.pending_changed = pending_counts.append
    await sess.toggle_async()
    stop = asyncio.create_task(sess.toggle_async())
    await asyncio.wait_for(transform.started.wait(), 1)
    await asyncio.wait_for(sess.toggle_async(), 1)
    capture = FakeCapture.instances[-1]
    assert sess.state is SessionState.RECORDING
    assert pending_counts[-1] == 1
    states_before = list(rec.states)

    transform.release.set()
    await asyncio.wait_for(stop, 1)
    assert rec.finished[0].final_text == "TRANSFORMED"
    assert sess.state is SessionState.RECORDING
    assert rec.states == states_before, "background completion cannot reset listening"
    assert capture.started
    assert pending_counts[-1] == 0
    await sess.shutdown()


async def test_shutdown_cancels_pending_work_without_late_output():
    stt = GatedTranscriber()
    sess, rec, _ = build(transcriber=stt)
    await sess.toggle_async()
    stop = asyncio.create_task(sess.toggle_async())
    await asyncio.wait_for(stt.started[0].wait(), 1)
    await sess.toggle_async()
    await asyncio.wait_for(sess.shutdown(), 1)
    await asyncio.gather(stop, return_exceptions=True)
    stt.release[0].set()
    await asyncio.sleep(0)
    assert not any(capture.started for capture in FakeCapture.instances)
    assert rec.finished == []
    assert rec.delivered == []
    assert sess.state is SessionState.IDLE


async def test_failed_earlier_dictation_does_not_block_the_next():
    class FailingFirst(GatedTranscriber):
        async def transcribe(self, wav_bytes, opts):
            index = self.batch_calls
            result = await super().transcribe(wav_bytes, opts)
            if index == 0:
                raise RuntimeError("first request failed")
            return result

    stt = FailingFirst()
    settings = Settings(output=OutputSettings(paste_mode="after_transcription"))
    sess, rec, _ = build(settings=settings, transcriber=stt)
    await sess.toggle_async()
    first = asyncio.create_task(sess.toggle_async())
    await asyncio.wait_for(stt.started[0].wait(), 1)
    await sess.toggle_async()
    second = asyncio.create_task(sess.toggle_async())
    await asyncio.wait_for(stt.started[1].wait(), 1)
    stt.release[1].set()
    stt.release[0].set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert any("first request failed" in error for error in rec.errors)
    assert [text for text, _ in rec.delivered] == ["second"]
    assert sess.state is SessionState.IDLE


async def test_previous_live_stream_cannot_publish_into_new_recording():
    class LateStream(FakeStream):
        def __init__(self):
            super().__init__(final="first")
            self.finishing = asyncio.Event()
            self.release = asyncio.Event()
            self.late_delta = asyncio.Event()
            self.delta_sent = asyncio.Event()

        async def deltas(self):
            yield "first live"
            await self.late_delta.wait()
            yield " old recording"
            self.delta_sent.set()

        async def finish(self):
            self.finishing.set()
            await self.release.wait()
            return await super().finish()

    first_stream = LateStream()
    second_stream = FakeStream(deltas=["second live"], final="second")

    class Streams(FakeTranscriber):
        async def open_stream(self, opts):
            return streams.pop(0)

    streams = [first_stream, second_stream]
    sess, rec, _ = build(transcriber=Streams(streaming=True))
    await sess.toggle_async()
    first = asyncio.create_task(sess.toggle_async())
    await asyncio.wait_for(first_stream.finishing.wait(), 1)
    await sess.toggle_async()
    await asyncio.sleep(0)  # open the second stream
    await asyncio.sleep(0)  # read its live deltas
    assert rec.partials[-1] == "second live"
    partials = list(rec.partials)
    first_stream.late_delta.set()
    await asyncio.wait_for(first_stream.delta_sent.wait(), 1)
    assert rec.partials == partials, "late words from A cannot appear in B's overlay"
    first_stream.release.set()
    await asyncio.wait_for(first, 1)
    assert sess.state is SessionState.RECORDING
    await sess.toggle_async()
    assert [result.transcript for result in rec.finished] == ["first", "second"]
    assert all(result.streamed for result in rec.finished)


async def test_already_queued_shortcuts_release_microphone_before_reopening(monkeypatch):
    original_start = FakeCapture.start

    def exclusive_start(capture):
        assert not any(other.started for other in FakeCapture.instances if other is not capture)
        original_start(capture)

    monkeypatch.setattr(FakeCapture, "start", exclusive_start)
    sess, rec, _ = build(transcriber=FakeTranscriber())
    await asyncio.gather(*(sess.toggle_async() for _ in range(3)))
    assert len(FakeCapture.instances) == 2
    assert sess.state is SessionState.RECORDING
    assert len(rec.finished) == 1
    assert rec.states.count(SessionState.RECORDING) == 2, "each recording resets the overlay"
    await sess.shutdown()
