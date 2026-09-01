"""Hotkey combos and the backend contract.

A combo is parsed once into a neutral form and then rendered per backend, because the three
platforms name keys differently: Win32 wants virtual-key codes, the XDG portal wants XKB
keysym names, and the "external" backend just needs something to show the user.

`Ctrl+Shift+;` is the default. Semicolon is the interesting case -- Win32 calls it
`VK_OEM_1` (0xBA) and XKB calls it `semicolon`, and neither is guessable from the character.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

TriggerCallback = Callable[[], None]

_MOD_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "ctl": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "option": "alt",
    "meta": "meta",
    "super": "meta",
    "win": "meta",
    "cmd": "meta",
}

# Punctuation and named keys: character -> (Win32 virtual-key, XKB keysym name).
# Only the keys worth binding a push-to-talk shortcut to; extended on demand rather than
# transcribing the whole keyboard.
_KEY_TABLE: dict[str, tuple[int, str]] = {
    ";": (0xBA, "semicolon"),
    "'": (0xDE, "apostrophe"),
    ",": (0xBC, "comma"),
    ".": (0xBE, "period"),
    "/": (0xBF, "slash"),
    "\\": (0xDC, "backslash"),
    "[": (0xDB, "bracketleft"),
    "]": (0xDD, "bracketright"),
    "-": (0xBD, "minus"),
    "=": (0xBB, "equal"),
    "`": (0xC0, "grave"),
    "space": (0x20, "space"),
    "tab": (0x09, "Tab"),
    "enter": (0x0D, "Return"),
    "insert": (0x2D, "Insert"),
    "delete": (0x2E, "Delete"),
    "home": (0x24, "Home"),
    "end": (0x23, "End"),
    "pageup": (0x21, "Prior"),
    "pagedown": (0x22, "Next"),
    "up": (0x26, "Up"),
    "down": (0x28, "Down"),
    "left": (0x25, "Left"),
    "right": (0x27, "Right"),
}


class InvalidCombo(ValueError):
    """The combo string could not be parsed. Message is safe to show the user."""


@dataclass(frozen=True, slots=True)
class Combo:
    key: str
    ctrl: bool = False
    shift: bool = False
    alt: bool = False
    meta: bool = False

    @classmethod
    def parse(cls, text: str) -> Combo:
        parts = [p.strip().lower() for p in text.split("+") if p.strip()]
        if not parts:
            raise InvalidCombo("Empty shortcut.")
        mods = {"ctrl": False, "shift": False, "alt": False, "meta": False}
        key: str | None = None
        for part in parts:
            if part in _MOD_ALIASES:
                mods[_MOD_ALIASES[part]] = True
            elif key is None:
                key = part
            else:
                raise InvalidCombo(f"More than one non-modifier key in {text!r}.")
        if key is None:
            raise InvalidCombo(f"{text!r} has modifiers but no key.")
        if not (key in _KEY_TABLE or len(key) == 1 or _is_function_key(key)):
            raise InvalidCombo(f"Unrecognised key {key!r}.")
        if not any(mods.values()):
            # A bare key would fire while typing. Refusing is kinder than letting someone
            # bind "s" and then wonder why every word starts a recording.
            raise InvalidCombo("A global shortcut needs at least one modifier.")
        return cls(key=key, **mods)

    def __str__(self) -> str:
        order = [
            ("ctrl", self.ctrl),
            ("shift", self.shift),
            ("alt", self.alt),
            ("meta", self.meta),
        ]
        return "+".join([name for name, on in order if on] + [self.key])

    @property
    def display(self) -> str:
        """Title-cased for the UI: 'Ctrl+Shift+;'."""
        order = [
            ("Ctrl", self.ctrl),
            ("Shift", self.shift),
            ("Alt", self.alt),
            ("Super", self.meta),
        ]
        key = self.key if len(self.key) == 1 else self.key.title()
        return "+".join([name for name, on in order if on] + [key])

    # -- per-backend rendering ----------------------------------------------------------

    def to_win32(self) -> tuple[int, int]:
        """(fsModifiers, virtual-key) for RegisterHotKey."""
        mod = 0
        if self.alt:
            mod |= 0x0001  # MOD_ALT
        if self.ctrl:
            mod |= 0x0002  # MOD_CONTROL
        if self.shift:
            mod |= 0x0004  # MOD_SHIFT
        if self.meta:
            mod |= 0x0008  # MOD_WIN
        # MOD_NOREPEAT: holding the combo must not fire repeatedly, or a held key would
        # start and stop recording dozens of times.
        mod |= 0x4000
        return mod, self._vk()

    def _vk(self) -> int:
        if entry := _KEY_TABLE.get(self.key):
            return entry[0]
        if _is_function_key(self.key):
            return 0x70 + int(self.key[1:]) - 1
        if len(self.key) == 1:
            return ord(self.key.upper())
        raise InvalidCombo(f"No virtual-key code for {self.key!r}.")

    def to_xdg(self) -> str:
        """Trigger string for the XDG GlobalShortcuts portal, e.g. 'CTRL+SHIFT+semicolon'."""
        mods = [
            name
            for name, on in (
                ("CTRL", self.ctrl),
                ("SHIFT", self.shift),
                ("ALT", self.alt),
                ("SUPER", self.meta),
            )
            if on
        ]
        return "+".join([*mods, self._xkb()])

    def _xkb(self) -> str:
        if entry := _KEY_TABLE.get(self.key):
            return entry[1]
        if _is_function_key(self.key):
            return self.key.upper()
        return self.key


def _is_function_key(key: str) -> bool:
    return len(key) >= 2 and key[0] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24


# --------------------------------------------------------------------------------------
# Backend contract
# --------------------------------------------------------------------------------------


@runtime_checkable
class HotkeyBackend(Protocol):
    """A source of global trigger events.

    `start` must not block. `on_trigger` may be called from any thread, so implementations
    of it must marshal onto the UI thread themselves.
    """

    name: str

    @staticmethod
    def available() -> bool:
        """Whether this backend can work in the current session."""
        ...

    def start(self, combo: Combo, on_trigger: TriggerCallback) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> str:
        """One line for the settings pane: is the shortcut actually live, and if not, why."""
        ...
