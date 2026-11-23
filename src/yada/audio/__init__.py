"""Microphone capture and the one-tap-many-sinks fan-out."""

from .buffer import BYTES_PER_SECOND, RecordingTooLong, WavBuffer
from .capture import (
    AudioCapture,
    AudioDeviceError,
    DeviceInfo,
    find_input_device_name,
    list_input_devices,
    peak_level,
    warm_up,
    without_slot,
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
    "find_input_device_name",
    "list_input_devices",
    "peak_level",
    "warm_up",
    "without_slot",
]
