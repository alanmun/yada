"""Microphone capture and the one-tap-many-sinks fan-out."""

from .buffer import BYTES_PER_SECOND, RecordingTooLong, WavBuffer
from .capture import (
    AudioCapture,
    AudioDeviceError,
    DeviceInfo,
    list_input_devices,
    peak_level,
    warm_up,
)
from .tee import AudioTee, StreamSink

__all__ = [
    "BYTES_PER_SECOND",
    "AudioCapture",
    "AudioDeviceError",
    "AudioTee",
    "DeviceInfo",
    "RecordingTooLong",
    "StreamSink",
    "WavBuffer",
    "list_input_devices",
    "peak_level",
    "warm_up",
]
