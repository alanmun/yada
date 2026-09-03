"""One entry per physical microphone.

PortAudio enumerates a device once per host API, so on Windows the same microphone appears
two to four times -- WASAPI, DirectSound, WDM-KS, MME -- and the dropdown reads as every
microphone being duplicated. MME also truncates names to 31 characters, which is why a
stored device name can be cut off mid-word.
"""

from __future__ import annotations

import sys

import pytest

from yada.audio import capture


def _fake_sounddevice(monkeypatch, devices, hostapis, default_in=0):
    class FakeSd:
        class default:  # lowercase to mirror sounddevice's own shape
            device = (default_in, None)

        @staticmethod
        def query_devices():
            return devices

        @staticmethod
        def query_hostapis():
            return [{"name": name} for name in hostapis]

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


def test_the_best_host_api_wins_the_stream(monkeypatch):
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


def test_the_system_default_survives_deduplication(monkeypatch):
    """Its flag must not be lost with the enumeration it happened to come from."""
    _fake_sounddevice(monkeypatch, WINDOWS_LIKE, HOSTAPIS, default_in=0)
    rode = capture.list_input_devices()[0]
    assert rode.is_default is True


def test_host_api_preference_order():
    assert (
        capture._hostapi_rank("Windows WASAPI")
        < capture._hostapi_rank("Windows DirectSound")
        < capture._hostapi_rank("Windows WDM-KS")
        < capture._hostapi_rank("ALSA")  # unknown
        < capture._hostapi_rank("MME")  # legacy, and the one that truncates
    )


def test_no_audio_stack_is_not_an_error(monkeypatch):
    """A missing PortAudio is the documented "container or fresh WSL" case, not a failure."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("no sounddevice")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    assert capture.list_input_devices() == []
