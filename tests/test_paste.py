"""The INPUT structure has to be exactly the size Windows expects.

`cbSize` not matching `sizeof(INPUT)` is a documented hard failure, and the size is set by
the union's largest member -- MOUSEINPUT -- which was missing. The structure came out at 32
bytes where Windows requires 40, so SendInput returned 0 with ERROR_INVALID_PARAMETER and
injected nothing: auto-paste never worked on Windows, on any release.

Nothing about that is visible without checking the number, which is why it is checked here.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="SendInput and ctypes.wintypes are Windows-only"
)


def test_the_input_structure_is_the_size_windows_requires():
    import ctypes

    from yada.output.paste import _win32_input

    _user32, INPUT, _event = _win32_input()
    assert ctypes.sizeof(INPUT) == 40, (
        f"INPUT is {ctypes.sizeof(INPUT)} bytes; SendInput fails with "
        "ERROR_INVALID_PARAMETER unless cbSize is exactly 40 on x64"
    )


def test_the_union_carries_every_member_so_the_size_cannot_drift():
    """Declaring only KEYBDINPUT is what made it too small in the first place."""
    from yada.output.paste import _win32_input

    _user32, INPUT, _event = _win32_input()
    union = dict(INPUT._fields_)["u"]
    assert {name for name, _ in union._fields_} == {"mi", "ki", "hi"}


def test_a_keyboard_event_is_built_without_a_pointer_for_extra_info():
    """dwExtraInfo is ULONG_PTR. A pointer type happens to be the right width and is not
    the right type; passing None through it is how it stayed unnoticed."""
    from yada.output.paste import _win32_input

    _user32, _INPUT, keyboard_event = _win32_input()
    event = keyboard_event(0x56, up=False)
    assert event.type == 1
    assert event.ki.wVk == 0x56
    assert event.ki.dwExtraInfo == 0

    released = keyboard_event(0x56, up=True)
    assert released.ki.dwFlags == 0x0002


def test_sendinput_accepts_the_structure():
    """The end-to-end proof, using a key nothing listens to (VK_F24)."""
    import ctypes

    from yada.output.paste import _win32_input

    user32, INPUT, keyboard_event = _win32_input()
    sequence = (INPUT * 2)(keyboard_event(0x87, up=False), keyboard_event(0x87, up=True))
    ctypes.set_last_error(0)
    sent = user32.SendInput(2, ctypes.byref(sequence), ctypes.sizeof(INPUT))
    assert sent == 2, f"SendInput rejected the structure, GetLastError={ctypes.get_last_error()}"
