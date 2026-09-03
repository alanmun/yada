"""`yada doctor` — check whether this machine can actually run yada.

Exists because the interesting failures are environmental and silent: no PortAudio, no tray,
no keyring, a Wayland session that will not let an app grab keys. Each of those produces an
app that starts and then does not work, which is much harder to diagnose than a crash.

Every check reports what it found and, when something is missing, the specific command to fix
it. Nothing here touches the network or costs money.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

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


def _probe_command() -> list[str]:
    """How to re-invoke ourselves for the out-of-process tray probe."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--probe-tray"]
    return [sys.executable, "-m", "yada", "--probe-tray"]


def _qt_checks() -> list[Check]:
    """Whether a system tray exists, asked in a child process.

    This looks like overkill for one boolean, and it is not. Answering requires a live
    QApplication; constructing one touches the window system and, on Windows in a
    non-interactive session, can block inside a Win32 call while holding the GIL. That
    freezes every thread in the process, so a thread-based timeout cannot help -- the
    symptom is `yada doctor` printing nothing and never returning. A child process can
    simply be killed.
    """
    try:
        proc = subprocess.run(
            _probe_command(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [
            Check(
                "Tray icon",
                WARN,
                "could not determine tray availability (the probe did not respond)",
                "Qt could not initialise in this session. yada may still run; if the tray "
                "icon never appears, this is why.",
            )
        ]
    except OSError as exc:
        return [Check("Tray icon", WARN, f"could not run the tray probe ({exc})")]

    verdict = (proc.stdout or "").strip()
    if verdict == "1":
        return [Check("Tray icon", OK, "a system tray is available")]
    if verdict == "0":
        return [
            Check(
                "Tray icon",
                WARN,
                "no system tray in this session",
                "On GNOME install the AppIndicator extension. On KDE the tray is built in. "
                "yada still works via its shortcut, but you will not see an icon.",
            )
        ]
    detail = (proc.stderr or "").strip()[:160] or f"exit {proc.returncode}"
    return [Check("Tray icon", WARN, f"tray probe failed: {detail}")]


def _hotkey_checks() -> list[Check]:
    from .hotkey import Combo, InvalidCombo, available_backends, create_backend, toggle_command

    settings = config.load()
    try:
        combo = Combo.parse(settings.hotkey.combo)
        combo_note = combo.display
    except InvalidCombo as exc:
        return [
            Check(
                "Shortcut",
                FAIL,
                f"{settings.hotkey.combo!r} is invalid: {exc}",
                "Fix it on the Shortcut tab in Settings.",
            )
        ]

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
    from .updater.core import (
        KEEP_VERSIONS,
        install_root,
        installed_versions,
        read_current,
    )
    from .updater.github import RELEASE_PUBLIC_KEY_B64

    checks: list[Check] = []
    versions = installed_versions()
    current = read_current()
    if versions:

        def size_mb(path: Path) -> int:
            total = 0
            for item in path.rglob("*"):
                if item.is_file():
                    with contextlib.suppress(OSError):
                        total += item.stat().st_size
            return round(total / (1024 * 1024))

        listed = ", ".join(
            f"{v.version}{'*' if v.version == current else ''} ({size_mb(v.path)} MB)"
            for v in versions
        )
        note = f"{listed}  (* = active) in {install_root()}"
        # A version directory is around 190 MB, and only the newest couple are kept. Worth
        # showing, because "why is this app 400 MB" deserves an answer on screen.
        if len(versions) > KEEP_VERSIONS:
            checks.append(
                Check(
                    "Installed versions",
                    WARN,
                    f"{note} — more than the {KEEP_VERSIONS} yada keeps",
                    "The extra copies are pruned the next time yada starts. The active "
                    "version is never removed, so a stale 'current' pointer can pin an "
                    "old one.",
                )
            )
        else:
            checks.append(Check("Installed versions", OK, note))
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


def _antivirus_checks() -> list[Check]:
    """On Windows, look for antivirus action against yada's own files.

    Worth a dedicated check because the symptom is otherwise baffling: yada installs
    cleanly, then a minute later its shortcut and autostart entry are gone and nothing
    starts. That happened for real -- Defender classified the old one-file launcher as
    Trojan:Win32/Bearfoos.A!ml, a machine-learning heuristic that fires readily on
    self-extracting executables, and removed the file, the Start Menu shortcut and the run
    key together. yada no longer ships that binary, but if anything else gets quarantined
    the user deserves to be told rather than left guessing.
    """
    if sys.platform != "win32":
        return []
    script = (
        "$d = Get-MpThreatDetection -ErrorAction SilentlyContinue | "
        "Where-Object { ($_.Resources -join ' ') -match 'yada' } | "
        "Sort-Object InitialDetectionTime -Descending | Select-Object -First 1; "
        "if ($d) { "
        "  $t = Get-MpThreat -ErrorAction SilentlyContinue | "
        "       Where-Object { $_.ThreatID -eq $d.ThreatID } | Select-Object -First 1; "
        '  "$($t.ThreatName)|$($d.InitialDetectionTime)" }'
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            # Deliberately inside CHECK_TIMEOUT_SECONDS. At 25s against a 20s group
            # deadline the group was always abandoned first, so this timeout could never
            # fire and its own error handling was dead code -- while Defender's history,
            # which is slow to enumerate on a machine with any, burned the whole budget.
            timeout=CHECK_TIMEOUT_SECONDS * 0.6,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    line = (proc.stdout or "").strip()
    if not line or "|" not in line:
        return [Check("Antivirus", OK, "no antivirus action against yada's files")]
    name, _, when = line.partition("|")
    return [
        Check(
            "Antivirus",
            WARN,
            f"Windows Defender acted on a yada file: {name.strip()} ({when.strip()})",
            "This is a false positive -- yada is unsigned, and heuristics flag small "
            "unsigned programs that start with Windows. To keep it working, either allow "
            "the item in Windows Security > Protection history, or exclude the folder:\n"
            '    Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\\yada"\n'
            "(that command needs an administrator PowerShell)",
        )
    ]


def _path_checks() -> list[Check]:
    from .providers.catalog import ModelCatalog

    catalog = ModelCatalog()
    lines = [f"settings:  {config.config_path()}", f"cache:     {catalog.path}"]
    for pid in SPECS:
        entry = catalog.entry(pid)
        if entry.fetched_at:
            lines.append(f"{pid}: {entry.staleness_note()}")
    return [Check("Paths", OK, "\n      ".join(lines))]


# Some checks talk to hardware or the window system and can block indefinitely:
# PortAudio device enumeration and QApplication construction are both capable of it,
# depending on drivers and session type. A diagnostic tool that hangs is worse than no
# tool at all, so every group runs with a deadline.
CHECK_TIMEOUT_SECONDS = 20.0

GROUPS: tuple[tuple[str, Callable[[], list[Check]]], ...] = ()


def _run_group(name: str, group: Callable[[], list[Check]]) -> list[Check]:
    """Run one group on a daemon thread and give up on it if it stalls.

    A daemon thread cannot be killed, but it also cannot keep the process alive, so a
    wedged driver call costs one timed-out line rather than a hung command.
    """
    result: list[Check] = []
    error: list[BaseException] = []

    def target() -> None:
        try:
            result.extend(group())
        except BaseException as exc:  # noqa: BLE001 - report, never propagate
            error.append(exc)

    thread = threading.Thread(target=target, name=f"doctor-{name}", daemon=True)
    thread.start()
    thread.join(CHECK_TIMEOUT_SECONDS)

    if thread.is_alive():
        return [
            Check(
                name,
                FAIL,
                f"check did not finish within {CHECK_TIMEOUT_SECONDS:.0f}s and was abandoned",
                "This usually means a driver or the window system is not responding. "
                "The rest of the report below is still valid.",
            )
        ]
    if error:
        return [Check(name, FAIL, f"check failed: {error[0]}")]
    return result


# Nine groups at CHECK_TIMEOUT_SECONDS each is three minutes, which is not a diagnostic
# tool, it is a hang. Past this the rest are reported as skipped rather than waited for.
TOTAL_BUDGET_SECONDS = 60.0


def iter_checks() -> Iterator[Check]:
    """Yield checks as they complete, so output appears even if a later one stalls."""
    import time

    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    for name, group in (
        ("Platform", _platform_checks),
        ("Microphone", _audio_checks),
        ("Tray icon", _qt_checks),
        ("Shortcut", _hotkey_checks),
        ("Auto-paste", _paste_checks),
        ("Credentials", _credential_checks),
        ("Install", _install_checks),
        ("Antivirus", _antivirus_checks),
        ("Paths", _path_checks),
    ):
        if time.monotonic() >= deadline:
            yield Check(
                name,
                WARN,
                "skipped — doctor ran out of time before reaching this check",
                "Something earlier stalled. The checks above name what completed.",
            )
            continue
        yield from _run_group(name, group)


def run_checks() -> list[Check]:
    return list(iter_checks())


def report_path() -> Path:
    """Where the report is always written, whether or not anything can be printed."""
    from .updater.core import install_root

    return install_root() / "doctor-report.txt"


class _Emitter:
    """Writes every line to a file, and to stdout when there is one.

    Both halves are needed. A PyInstaller windowed build has no stdout unless it manages
    to attach to a parent console, so `yada doctor` redirected to a file produced an empty
    file -- the one tool for diagnosing "it will not start" was unusable in precisely the
    situation it exists for. And writing as it goes means a check that stalls still leaves
    a report naming the check it stalled on, instead of nothing at all.
    """

    def __init__(self, path: Path) -> None:
        self._file = None
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("w", encoding="utf-8", errors="replace", buffering=1)

    def __call__(self, line: str = "") -> None:
        if self._file is not None:
            with contextlib.suppress(OSError, ValueError):
                self._file.write(line + "\n")
        # stdout is absent in a windowed build, and print() on it raises rather than
        # discarding, which would abandon the report half-written.
        with contextlib.suppress(Exception):
            print(line, flush=True)

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError, ValueError):
                self._file.close()


def main() -> int:
    # Header first, then a line per check as it finishes. If something stalls, the report
    # already names everything that passed and stops at the culprit.
    path = report_path()
    emit = _Emitter(path)
    try:
        emit("yada doctor")
        emit(f"report: {path}")
        emit()
        checks: list[Check] = []
        for check in iter_checks():
            checks.append(check)
            emit(f"[{MARKS[check.status]}] {check.name}: {check.detail}")
            if check.fix:
                for line in check.fix.splitlines():
                    emit(f"          {line}")
        failures = [c for c in checks if c.status == FAIL]
        warnings = [c for c in checks if c.status == WARN]
        emit()
        if failures:
            emit(f"{len(failures)} problem(s) will stop yada working. Fix those first.")
            # Shipping a green summary next to a red line would be worse than useless.
            if shutil.which("uv") is None and sys.platform != "win32":
                emit("Note: uv was not found on PATH.")
            return 1
        if warnings:
            emit(f"Usable, with {len(warnings)} thing(s) worth knowing about above.")
            return 0
        emit("Everything checks out.")
        return 0
    finally:
        emit.close()
