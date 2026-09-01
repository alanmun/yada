"""Delivering the result: clipboard, optional paste, and the two chimes."""

from .chime import ChimePlayer
from .clipboard import copy
from .paste import (
    NoPasteBackend,
    PasteBackend,
    Win32PasteBackend,
    YdotoolPasteBackend,
    create_paste_backend,
    paste_available,
)

__all__ = [
    "ChimePlayer",
    "NoPasteBackend",
    "PasteBackend",
    "Win32PasteBackend",
    "YdotoolPasteBackend",
    "copy",
    "create_paste_backend",
    "paste_available",
]
