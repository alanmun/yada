"""One entry per physical microphone.

PortAudio enumerates a device once per host API, so on Windows the same microphone appears
two to four times -- WASAPI, DirectSound, WDM-KS, MME -- and the dropdown reads as every
microphone being duplicated. MME also truncates names to 31 characters, which is why a
stored device name can be cut off mid-word.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from _sounddevice import ffi

from yada.audio import capture


def _fake_sounddevice(monkeypatch, devices, hostapis, default_in=0, wasapi_default=1):
    class FakeSd:
        _ffi = ffi

        class default:  # lowercase to mirror sounddevice's own shape
            device = (default_in, None)

        @staticmethod
        def query_devices(index=None, kind=None):
            if index is None:
                return devices
            assert index >= 0, "an unavailable default must not be passed to PortAudio"
            return devices[index]

        @staticmethod
        def query_hostapis():
            return [
                {
                    "name": name,
                    "default_input_device": wasapi_default if name == "Windows WASAPI" else -1,
                }
                for name in hostapis
            ]

    monkeypatch.setitem(sys.modules, "sounddevice", FakeSd)


WINDOWS_LIKE = [
    # The RØDE, as Windows really presents it: same microphone, four host APIs, and MME
    # cutting the name off at 31 characters.
    {
        "name": "Desktop Microphone (RØDE PodMic",
        "max_input_channels": 1,
        "default_samplerate": 44100.0,
        "hostapi": 0,
    },
    {
        "name": "Desktop Microphone (RØDE PodMic)",
        "max_input_channels": 2,
        "default_samplerate": 48000.0,
        "hostapi": 1,
    },
    {
        "name": "Desktop Microphone (RØDE PodMic)",
        "max_input_channels": 2,
        "default_samplerate": 48000.0,
        "hostapi": 2,
    },
    {
        "name": "Line In (Realtek Audio)",
        "max_input_channels": 2,
        "default_samplerate": 48000.0,
        "hostapi": 1,
    },
    {
        "name": "Speakers (Realtek Audio)",
        "max_input_channels": 0,
        "default_samplerate": 48000.0,
        "hostapi": 1,
    },
]
HOSTAPIS = ["MME", "Windows WASAPI", "Windows DirectSound"]


@pytest.fixture(autouse=True)
def _on_windows(monkeypatch):
    """The truncation tolerance is Windows-only, so the test has to be too."""
    monkeypatch.setattr(capture.sys, "platform", "win32")


def test_one_entry_per_microphone(monkeypatch):
    _fake_sounddevice(monkeypatch, WINDOWS_LIKE, HOSTAPIS)
    devices = capture.list_input_devices()
    assert [d.name for d in devices] == [
        "Desktop Microphone (RØDE PodMic)",
        "Line In (Realtek Audio)",
    ], "four enumerations of one microphone must collapse to one"


def test_the_fullest_name_is_kept(monkeypatch):
    """MME's truncated name is never what gets shown, even when MME comes first."""
    _fake_sounddevice(monkeypatch, WINDOWS_LIKE, HOSTAPIS)
    rode = capture.list_input_devices()[0]
    assert rode.name.endswith(")"), "the truncated MME name would be missing its bracket"


def test_windows_uses_wasapi_device_properties(monkeypatch):
    _fake_sounddevice(monkeypatch, WINDOWS_LIKE, HOSTAPIS)
    rode = capture.list_input_devices()[0]
    assert rode.hostapi == "Windows WASAPI"
    assert rode.channels == 2, "and its properties, not MME's"


def test_a_name_stored_before_deduplication_still_resolves(monkeypatch):
    """Existing settings hold MME's truncated name; it must not silently reassign them."""
    _fake_sounddevice(monkeypatch, WINDOWS_LIKE, HOSTAPIS)
    index = capture.resolve_device("Desktop Microphone (RØDE PodMic")
    assert index is not None
    assert index == capture.list_input_devices()[0].index


def test_an_unknown_device_still_falls_back_to_the_default(monkeypatch):
    _fake_sounddevice(monkeypatch, WINDOWS_LIKE, HOSTAPIS)
    assert capture.resolve_device("Some Unplugged Headset") is None


def test_outputs_are_not_offered_as_inputs(monkeypatch):
    _fake_sounddevice(monkeypatch, WINDOWS_LIKE, HOSTAPIS)
    assert not any("Speakers" in d.name for d in capture.list_input_devices())


def test_windows_picker_uses_wasapi_endpoints_and_keeps_distinct_long_names(monkeypatch):
    devices = [
        *WINDOWS_LIKE,
        dict(WINDOWS_LIKE[0], name="Microsoft Sound Mapper - Input"),
        dict(WINDOWS_LIKE[1], name="Desktop Microphone (RØDE PodMic 2)"),
        dict(WINDOWS_LIKE[1], name="PC Speaker (Realtek HD Audio output)", hostapi=3),
    ]
    _fake_sounddevice(monkeypatch, devices, [*HOSTAPIS, "Windows WDM-KS"])
    assert [d.index for d in capture.list_input_devices()] == [1, 3, 6]
    assert capture.resolve_device("Desktop Microphone (RØDE PodMic") is None


def test_windows_picker_does_not_offer_legacy_inputs_without_wasapi(monkeypatch):
    devices = [WINDOWS_LIKE[0], dict(WINDOWS_LIKE[2], hostapi=1)]
    _fake_sounddevice(monkeypatch, devices, ["MME", "Windows DirectSound"])
    assert capture.list_input_devices() == []


def test_windows_picker_excludes_wasapi_playback_loopback(monkeypatch):
    devices = [
        WINDOWS_LIKE[1],
        dict(WINDOWS_LIKE[1], name="Speaker output capture"),
    ]
    _fake_sounddevice(monkeypatch, devices, HOSTAPIS, wasapi_default=0)
    monkeypatch.setattr(
        sys.modules["sounddevice"],
        "_lib",
        SimpleNamespace(PaWasapi_IsLoopback=lambda index: int(index == 1)),
        raising=False,
    )
    assert [device.index for device in capture.list_input_devices()] == [0]


def test_windows_picker_handles_portaudio_without_a_loopback_symbol(monkeypatch):
    class PortAudio:
        @property
        def PaWasapi_IsLoopback(self):
            raise ffi.error("PaWasapi_IsLoopback symbol not found")

    _fake_sounddevice(monkeypatch, WINDOWS_LIKE, HOSTAPIS)
    sd = sys.modules["sounddevice"]
    monkeypatch.setattr(sd, "_lib", PortAudio(), raising=False)
    assert [device.index for device in capture.list_input_devices()] == [1, 3]


@pytest.mark.parametrize(
    ("device", "wasapi_default"), [(None, 1), ("Desktop Microphone (RØDE PodMic)", -1)]
)
def test_windows_capture_uses_the_wasapi_input(monkeypatch, device, wasapi_default):
    _fake_sounddevice(
        monkeypatch, WINDOWS_LIKE, HOSTAPIS, default_in=-1, wasapi_default=wasapi_default
    )
    opened = []

    def input_stream(**kwargs):
        stream = _FakeStream(**kwargs, ok_rates={48000})
        opened.append(stream)
        return stream

    monkeypatch.setattr(sys.modules["sounddevice"], "InputStream", input_stream, raising=False)
    monkeypatch.setattr(capture.AudioCapture, "_make_resampler", lambda self, rate: None)
    mic = capture.AudioCapture(lambda _: None, device=device)
    mic.start()
    mic.stop()
    assert [stream.device for stream in opened] == [1]


def test_windows_without_a_default_reports_how_to_select_one(monkeypatch):
    raw_device = dict(WINDOWS_LIKE[1], name="Microphone 1 (Realtek HD Audio)", hostapi=1)
    _fake_sounddevice(monkeypatch, [raw_device], ["Windows WASAPI", "Windows WDM-KS"], -1, -1)
    mic = capture.AudioCapture(lambda _: None)
    with pytest.raises(capture.AudioDeviceError, match="System > Sound > Input"):
        mic.start()
    assert capture.list_input_devices() == []


def test_picker_marks_the_wasapi_default(monkeypatch):
    _fake_sounddevice(monkeypatch, WINDOWS_LIKE, HOSTAPIS, default_in=0)
    rode = capture.list_input_devices()[0]
    assert rode.is_default is True


def test_a_duplicate_name_uses_the_actual_windows_default_index(monkeypatch):
    devices = [WINDOWS_LIKE[1], WINDOWS_LIKE[1]]
    _fake_sounddevice(monkeypatch, devices, HOSTAPIS, wasapi_default=1)
    (device,) = capture.list_input_devices()
    assert device.is_default and device.index == 1


@pytest.mark.parametrize("error", [ImportError, OSError, RuntimeError])
def test_no_audio_stack_is_not_an_error(monkeypatch, error):
    """A missing PortAudio is the documented "container or fresh WSL" case, not a failure."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise error("audio backend unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    assert capture.list_input_devices() == []


@pytest.mark.parametrize("error", [ImportError, OSError, RuntimeError])
def test_audio_initialization_failure_is_reported_when_recording(monkeypatch, error):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise error("audio backend unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    microphone = capture.AudioCapture(lambda _frames: None)
    with pytest.raises(capture.AudioDeviceError, match="No audio backend available"):
        microphone.start()


# What Linux showed after the audio service restarted: the webcam the user picked as
# "(hw:5,0)" had become hw:3,0, and hw:5,0 now belonged to a VR headset microphone that
# only runs at 48 kHz.
LINUX_RENUMBERED = [
    {
        "name": "HD Pro Webcam C920: USB Audio (hw:3,0)",
        "max_input_channels": 2,
        "default_samplerate": 32000.0,
        "hostapi": 0,
    },
    {
        "name": "Valve VR Radio & HMD Mic: USB Audio (hw:5,0)",
        "max_input_channels": 1,
        "default_samplerate": 48000.0,
        "hostapi": 0,
    },
]


def test_a_renumbered_alsa_device_is_still_found(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _fake_sounddevice(monkeypatch, LINUX_RENUMBERED, ["ALSA"])
    assert capture.resolve_device("HD Pro Webcam C920: USB Audio (hw:5,0)") == 0


class _FakeStream:
    def __init__(self, *, device, channels, samplerate, blocksize, dtype, callback, ok_rates):
        if samplerate not in ok_rates:
            raise RuntimeError("Error opening InputStream: Invalid sample rate")
        self.device, self.samplerate, self.closed = device, samplerate, False

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        self.closed = True


def _fake_linux_sd(monkeypatch, *, ok_rates):
    """A PortAudio whose device table is stale until it is re-initialised."""
    monkeypatch.setattr(sys, "platform", "linux")
    stale = [dict(LINUX_RENUMBERED[1], name="HD Pro Webcam C920: USB Audio (hw:5,0)")]
    state = {"devices": stale, "rescans": 0, "opened": []}

    class FakeSd:
        class default:
            device = (0, None)

        @staticmethod
        def _terminate():
            state["devices"] = LINUX_RENUMBERED
            state["rescans"] += 1

        @staticmethod
        def _initialize():
            pass

        @staticmethod
        def query_devices(index=None, kind=None):
            return state["devices"] if index is None else state["devices"][index]

        @staticmethod
        def query_hostapis():
            return [{"name": "ALSA"}]

        @staticmethod
        def InputStream(**kwargs):
            stream = _FakeStream(**kwargs, ok_rates=ok_rates)
            state["opened"].append(stream)
            return stream

    monkeypatch.setitem(sys.modules, "sounddevice", FakeSd)
    monkeypatch.setattr(capture.AudioCapture, "_make_resampler", lambda self, rate: None)
    return state


def test_recording_rescans_devices_and_opens_the_microphone_the_user_chose(monkeypatch):
    state = _fake_linux_sd(monkeypatch, ok_rates={32000})
    mic = capture.AudioCapture(lambda _: None, device="HD Pro Webcam C920: USB Audio (hw:5,0)")
    mic.start()
    assert state["rescans"] == 1
    assert state["opened"][-1].device == 0, "the webcam, not whatever took over hw:5,0"
    assert mic.input_samplerate == 32000
    mic.stop()


def test_a_device_rejecting_its_reported_rate_opens_at_another(monkeypatch):
    state = _fake_linux_sd(monkeypatch, ok_rates={48000})
    mic = capture.AudioCapture(lambda _: None, device="HD Pro Webcam C920: USB Audio (hw:3,0)")
    mic.start()
    assert mic.input_samplerate == 48000
    assert state["opened"][-1].device == 0
    mic.stop()


def test_no_rescan_while_another_stream_is_open(monkeypatch):
    state = _fake_linux_sd(monkeypatch, ok_rates={32000, 48000})
    first = capture.AudioCapture(lambda _: None)
    first.start()
    second = capture.AudioCapture(lambda _: None)
    second.start()
    assert state["rescans"] == 1, "re-initialising PortAudio would kill the first stream"
    second.stop()
    first.stop()
    third = capture.AudioCapture(lambda _: None)
    third.start()
    assert state["rescans"] == 2
    third.stop()
