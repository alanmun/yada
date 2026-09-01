"""Shortcut parsing and per-platform rendering.

The three platforms name keys differently, and semicolon -- the default -- is the case where
none of the three names is guessable from the character.
"""

from __future__ import annotations

import pytest

from yada.hotkey.base import Combo, InvalidCombo
from yada.hotkey.external import ExternalHotkeyBackend


def test_default_combo_parses():
    c = Combo.parse("ctrl+shift+;")
    assert (c.ctrl, c.shift, c.alt, c.meta, c.key) == (True, True, False, False, ";")
    assert c.display == "Ctrl+Shift+;"


def test_modifier_aliases():
    assert Combo.parse("control+shift+;") == Combo.parse("ctrl+shift+;")
    assert Combo.parse("super+a") == Combo.parse("win+a") == Combo.parse("meta+a")


def test_case_and_order_are_normalised():
    assert Combo.parse("SHIFT+CTRL+;") == Combo.parse("ctrl+shift+;")
    assert str(Combo.parse("shift+ctrl+;")) == "ctrl+shift+;"


def test_round_trip_through_string():
    for text in ("ctrl+shift+;", "alt+f5", "ctrl+alt+meta+space", "shift+ctrl+/"):
        c = Combo.parse(text)
        assert Combo.parse(str(c)) == c


def test_semicolon_renders_correctly_for_each_platform():
    c = Combo.parse("ctrl+shift+;")
    modifiers, vk = c.to_win32()
    assert vk == 0xBA, "semicolon is VK_OEM_1 on Windows"
    assert modifiers & 0x0002, "MOD_CONTROL"
    assert modifiers & 0x0004, "MOD_SHIFT"
    assert modifiers & 0x4000, "MOD_NOREPEAT, so holding the keys fires once"
    assert c.to_xdg() == "CTRL+SHIFT+semicolon", "XKB calls it 'semicolon'"


def test_function_keys():
    c = Combo.parse("alt+f5")
    assert c.to_win32()[1] == 0x74  # VK_F5
    assert c.to_xdg() == "ALT+F5"


def test_letter_keys():
    c = Combo.parse("ctrl+alt+d")
    assert c.to_win32()[1] == ord("D")
    assert c.to_xdg() == "CTRL+ALT+d"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        ("   ", "whitespace only"),
        ("ctrl", "no non-modifier key"),
        ("ctrl+shift", "no non-modifier key"),
        ("ctrl+a+b", "two non-modifier keys"),
        ("ctrl+nonsense", "unrecognised key name"),
    ],
)
def test_invalid_combos_are_rejected(text, reason):
    with pytest.raises(InvalidCombo):
        Combo.parse(text)


def test_bare_key_is_rejected():
    """Binding a modifier-free key would start a recording every time you typed it."""
    with pytest.raises(InvalidCombo, match="at least one modifier"):
        Combo.parse("s")


def test_external_backend_is_always_available_and_explains_itself():
    backend = ExternalHotkeyBackend()
    assert backend.available() is True
    backend.start(Combo.parse("ctrl+shift+;"), lambda: None)
    status = backend.status()
    assert "Ctrl+Shift+;" in status
    assert "toggle" in status, "must tell the user the exact command to bind"
    backend.stop()


def test_backend_selection_never_returns_none():
    from yada.hotkey import available_backends, create_backend

    assert "external" in available_backends()
    for preference in ("auto", "win32", "kde_portal", "external", "nonsense"):
        backend = create_backend(preference)
        assert backend is not None
        assert hasattr(backend, "start") and hasattr(backend, "status")
