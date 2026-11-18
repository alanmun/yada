"""Run the end-to-end tests inside a private, headless KDE Plasma session.

Linux has been yada's least-tested platform, and nearly every Wayland bug so far was
invisible to unit tests because each one lived in the conversation with the compositor:
a shortcut KWin never matched, a clipboard write the compositor silently refused, an
overlay KWin centred and focused. These tests run yada against a real KWin instead.

    uv run python scripts/e2e_kde.py              # all end-to-end tests
    uv run python scripts/e2e_kde.py -k paste     # extra arguments go to pytest

Requires kwin_wayland, xdg-desktop-portal(-kde), dbus-run-session and wl-clipboard -- a
stock Fedora KDE install has them all. Nothing touches the real desktop: the session gets
its own D-Bus, Wayland display, config, data and runtime directories, and renders to a
virtual output, so no window or dialog ever appears on screen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ("kwin_wayland", "dbus-run-session", "wl-copy", "wl-paste")


def _outside(pytest_args: list[str]) -> int:
    missing = [tool for tool in REQUIRED if not shutil.which(tool)]
    if missing:
        print(f"e2e: missing {', '.join(missing)}", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="yada-e2e-"))
    # Wayland socket paths are capped at 108 bytes, so the runtime dir must be short.
    runtime = Path(tempfile.mkdtemp(prefix="ye2e-", dir="/tmp"))
    runtime.chmod(0o700)
    try:
        env = _session_env(work, runtime)
        _install_desktop_file(Path(env["XDG_DATA_HOME"]))
        result = work / "exit-code"
        inner = [sys.executable, str(Path(__file__).resolve()), "--inside", str(result)]
        inner += pytest_args
        command = " ".join(_quote(part) for part in inner)
        subprocess.run(
            [
                "dbus-run-session",
                "--",
                "kwin_wayland",
                "--virtual",
                "--no-lockscreen",
                "--width",
                "1920",
                "--height",
                "1080",
                "--socket",
                "wayland-e2e",
                "--exit-with-session",
                command,
            ],
            env=env,
            cwd=ROOT,
            check=False,
            timeout=900,
        )
        # KWin's own exit status says nothing about the tests, so they report through a file.
        try:
            return int(result.read_text().strip())
        except (OSError, ValueError):
            print("e2e: the session ended without reporting a result", file=sys.stderr)
            return 3
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)


def _session_env(work: Path, runtime: Path) -> dict[str, str]:
    dirs = {name: work / name for name in ("config", "data", "cache", "state")}
    for path in dirs.values():
        path.mkdir(parents=True)
    keep = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "USER") if k in os.environ}
    return {
        **keep,
        "XDG_CONFIG_HOME": str(dirs["config"]),
        "XDG_DATA_HOME": str(dirs["data"]),
        "XDG_CACHE_HOME": str(dirs["cache"]),
        "XDG_STATE_HOME": str(dirs["state"]),
        "XDG_RUNTIME_DIR": str(runtime),
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "KDE",
        "KDE_FULL_SESSION": "true",
        "QT_QPA_PLATFORM": "wayland",
        "PYTHONPATH": str(ROOT / "src"),
        "YADA_E2E": "1",
    }


def _install_desktop_file(data_home: Path) -> None:
    """The portal only accepts an app id that names an installed desktop file."""
    apps = data_home / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "yada.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=yada\nExec=true\nNoDisplay=true\n",
        encoding="utf-8",
    )


def _inside(result: Path, pytest_args: list[str]) -> None:
    """Runs as KWin's session process: finish setting up, then run pytest."""
    code = 1
    try:
        # D-Bus-activated portals inherit the bus daemon's environment, which predates
        # the Wayland display; without this they cannot reach KWin.
        subprocess.run(
            [
                "dbus-update-activation-environment",
                "WAYLAND_DISPLAY",
                "XDG_CURRENT_DESKTOP",
                "XDG_SESSION_TYPE",
                "XDG_RUNTIME_DIR",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "XDG_STATE_HOME",
                "KDE_FULL_SESSION",
                "QT_QPA_PLATFORM",
            ],
            check=False,
        )
        code = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests/e2e", *pytest_args],
            cwd=ROOT,
            check=False,
        ).returncode
    finally:
        result.write_text(str(code), encoding="utf-8")


def _quote(part: str) -> str:
    import shlex

    return shlex.quote(part)


def main(argv: list[str]) -> int:
    if argv[:1] == ["--inside"]:
        _inside(Path(argv[1]), argv[2:])
        return 0
    return _outside(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
