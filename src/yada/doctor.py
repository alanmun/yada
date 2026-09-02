"""`yada doctor` — check whether this machine can actually run yada.

Exists because the interesting failures are environmental and silent: no PortAudio, no tray,
no keyring, a Wayland session that will not let an app grab keys. Each of those produces an
app that starts and then does not work, which is much harder to diagnose than a crash.

Every check reports what it found and, when something is missing, the specific command to fix
it. Nothing here touches the network or costs money.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

from . import config, secrets
from .providers.registry import SPECS

OK = "ok"
WARN = "warn"
FAIL = "fail"

MARKS = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _platform_checks() -> list[Check]:
    checks = [
        Check("Platform", OK, f"{sys.platform}, Python {sys.version.split()[0]}"),
    ]
    session = os.environ.get("XDG_SESSION_TYPE", "")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if sys.platform == "win32":
        checks.append(Check("Desktop", OK, "Windows — shortcut and auto-paste work natively"))
    elif "WSL" in os.uname().release if hasattr(os, "uname") else False:
        checks.append(
            Check(
                "Desktop",
                WARN,
                "WSL — no system tray or global shortcut here",
                "Run yada on Windows directly, or on a real Linux desktop. WSL is fine for "
                "development and tests, but not for using it.",
            )
        )
    elif session == "wayland":
        checks.append(
            Check(
                "Desktop",
                OK if "KDE" in desktop.upper() else WARN,
                f"Wayland ({desktop or 'unknown desktop'})",
                ""
                if "KDE" in desktop.upper()
                else "Outside KDE the GlobalShortcuts portal may be unavailable; yada will "
                "fall back to a desktop-bound command.",
            )
        )
    else:
        checks.append(Check("Desktop", OK, f"{session or 'X11'} ({desktop or 'unknown'})"))
    return checks


def _audio_checks() -> list[Check]:
    try:
        import sounddevice  # noqa: F401
    except OSError:
        return [
            Check(
                "Microphone",
                FAIL,
                "PortAudio is not installed — yada cannot record",
                "sudo apt install libportaudio2"
                if sys.platform != "win32"
                else "Reinstall yada; the Windows build bundles PortAudio.",
            )
        ]
    except ImportError as exc:
        return [Check("Microphone", FAIL, f"sounddevice is not installed ({exc})", "uv sync")]

    from .audio import list_input_devices

    devices = list_input_devices()
    if not devices:
        return [
            Check(
                "Microphone",
                FAIL,
                "PortAudio works but no input devices were found",
                "Check that a microphone is connected and not muted at the OS level.",
            )
        ]
    default = next((d for d in devices if d.is_default), devices[0])
    return [
        Check(
            "Microphone",
            OK,
            f"{len(devices)} input device(s); default is {default.name!r}",
        )
    ]


def _qt_checks() -> list[Check]:
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    except ImportError as exc:
        return [Check("Tray icon", FAIL, f"PySide6 is not installed ({exc})", "uv sync")]

    owned = QApplication.instance() is None
    app = QApplication([]) if owned else None
    try:
        available = QSystemTrayIcon.isSystemTrayAvailable()
    finally:
        if owned and app is not None:
            app.shutdown() if hasattr(app, "shutdown") else None

    if available:
        return [Check("Tray icon", OK, "a system tray is available")]
    return [
        Check(
            "Tray icon",
            WARN,
            "no system tray in this session",
            "On GNOME install the AppIndicator extension. On KDE the tray is built in. "
            "yada still works via its shortcut, but you will not see an icon.",
        )
    ]


def _hotkey_checks() -> list[Check]:
    from .hotkey import Combo, InvalidCombo, available_backends, create_backend, toggle_command

    settings = config.load()
    try:
        combo = Combo.parse(settings.hotkey.combo)
        combo_note = combo.display
    except InvalidCombo as exc:
        return [Check("Shortcut", FAIL, f"{settings.hotkey.combo!r} is invalid: {exc}",
                      "Fix it on the Shortcut tab in Settings.")]

    backend = create_backend(settings.hotkey.backend)
    checks = [
        Check(
            "Shortcut",
            OK,
            f"{combo_note} via the {backend.name!r} backend "
            f"(available: {', '.join(available_backends())})",
        )
    ]
    if backend.name == "external":
        checks.append(
            Check(
                "Shortcut binding",
                WARN,
                "the desktop must own this shortcut",
                f"Bind {combo_note} in System Settings → Shortcuts to:\n      {toggle_command()}",
            )
        )
    return checks


def _paste_checks() -> list[Check]:
    from .output import NoPasteBackend, create_paste_backend

    backend = create_paste_backend()
    if isinstance(backend, NoPasteBackend):
        return [
            Check(
                "Auto-paste",
                WARN,
                "unavailable — text will be copied to the clipboard only",
                "Optional. To enable it on Wayland:\n"
                "      sudo apt install ydotool\n"
                "      sudo systemctl enable --now ydotoold\n"
                "      sudo usermod -aG input $USER   (then log out and back in)",
            )
        ]
    return [Check("Auto-paste", OK, f"available via {backend.name}")]


def _credential_checks() -> list[Check]:
    checks = [Check("Key storage", OK, secrets.describe_store())]
    if secrets.file_store_is_permissive():
        checks.append(
            Check(
                "Key file permissions",
                FAIL,
                f"{secrets.credentials_path()} is readable by other accounts",
                f"chmod 600 {secrets.credentials_path()}",
            )
        )
    configured = []
    for pid, spec in SPECS.items():
        key, store = secrets.resolve_key(pid, spec.env_var)
        if key:
            configured.append(f"{spec.label} (from the {store})")
    if configured:
        checks.append(Check("API keys", OK, "; ".join(configured)))
    else:
        checks.append(
            Check(
                "API keys",
                FAIL,
                "no provider keys are configured — yada cannot transcribe anything",
                "Open Settings → Providers and paste a key, or export OPENAI_API_KEY.",
            )
        )
    return checks


def _install_checks() -> list[Check]:
    from .updater.core import install_root, installed_versions, read_current
    from .updater.github import RELEASE_PUBLIC_KEY_B64

    checks: list[Check] = []
    versions = installed_versions()
    current = read_current()
    if versions:
        listed = ", ".join(f"{v.version}{'*' if v.version == current else ''}" for v in versions)
        checks.append(
            Check("Installed versions", OK, f"{listed}  (* = active) in {install_root()}")
        )
    else:
        checks.append(
            Check("Installed versions", WARN, "running from source, not a managed install")
        )

    if RELEASE_PUBLIC_KEY_B64:
        checks.append(Check("Update signing key", OK, "a release public key is compiled in"))
    else:
        checks.append(
            Check(
                "Update signing key",
                WARN,
                "no release public key — auto-update will refuse every release",
                "Run scripts/gen_signing_key.py, put the private half in the "
                "YADA_SIGNING_KEY GitHub secret and the public half in "
                "src/yada/updater/github.py.",
            )
        )
    return checks


def _path_checks() -> list[Check]:
    from .providers.catalog import ModelCatalog

    catalog = ModelCatalog()
    lines = [f"settings:  {config.config_path()}", f"cache:     {catalog.path}"]
    for pid in SPECS:
        entry = catalog.entry(pid)
        if entry.fetched_at:
            lines.append(f"{pid}: {entry.staleness_note()}")
    return [Check("Paths", OK, "\n      ".join(lines))]


def run_checks() -> list[Check]:
    checks: list[Check] = []
    for group in (
        _platform_checks,
        _audio_checks,
        _qt_checks,
        _hotkey_checks,
        _paste_checks,
        _credential_checks,
        _install_checks,
        _path_checks,
    ):
        try:
            checks.extend(group())
        except Exception as exc:  # noqa: BLE001 - a broken check must not hide the others
            checks.append(Check(group.__name__.strip("_"), FAIL, f"check failed: {exc}"))
    return checks


def main() -> int:
    checks = run_checks()
    print("yada doctor\n")
    for check in checks:
        print(f"[{MARKS[check.status]}] {check.name}: {check.detail}")
        if check.fix:
            for line in check.fix.splitlines():
                print(f"          {line}")
    failures = [c for c in checks if c.status == FAIL]
    warnings = [c for c in checks if c.status == WARN]
    print()
    if failures:
        print(f"{len(failures)} problem(s) will stop yada working. Fix those first.")
        # Shipping a green summary next to a red line would be worse than useless.
        if shutil.which("uv") is None and sys.platform != "win32":
            print("Note: uv was not found on PATH.")
        return 1
    if warnings:
        print(f"Usable, with {len(warnings)} thing(s) worth knowing about above.")
        return 0
    print("Everything checks out.")
    return 0
