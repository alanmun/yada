"""Fixtures for tests that run against a real KWin. See scripts/e2e_kde.py.

Skipped entirely unless that script started the session: these tests drive a compositor,
portals and the clipboard, and must never run against a developer's own desktop.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPORT_BUS = "org.yada.E2E"
TARGET_TITLE = "yada e2e target"


def pytest_collection_modifyitems(config, items):
    if os.environ.get("YADA_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="end-to-end: run scripts/e2e_kde.py")
    for item in items:
        if "tests/e2e/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)


class Reports:
    """Messages KWin scripts send with callDBus; KWin's print() is filtered from logs."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def since(self, mark: int) -> list[str]:
        return self.lines[mark:]

    async def wait_for(self, predicate, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            for line in self.lines[seen:]:
                if predicate(line):
                    return line
            seen = len(self.lines)
            await asyncio.sleep(0.05)
        raise AssertionError(f"no matching report within {timeout}s; got {self.lines[-10:]}")


@pytest.fixture
async def reports():
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method

    collected = Reports()

    class Reporter(ServiceInterface):
        def __init__(self):
            super().__init__(REPORT_BUS)

        @method()
        def Report(self, line: "s") -> None:  # noqa: F821 - dbus-fast reads "s" as the signature
            collected.lines.append(line)

    bus = await MessageBus().connect()
    bus.export("/", Reporter())
    await bus.request_name(REPORT_BUS)
    yield collected
    bus.disconnect()


@pytest.fixture
def kwin_script():
    """Load a KWin script for the length of a test. Returns a loader."""
    loaded: list[str] = []

    def load(name: str, source: str) -> None:
        path = Path(os.environ["XDG_RUNTIME_DIR"]) / f"{name}.js"
        path.write_text(source, encoding="utf-8")
        _kwin("unloadScript", "s", name)
        _kwin("loadScript", "ss", str(path), name)
        _kwin("start", "")
        loaded.append(name)

    yield load
    for name in loaded:
        _kwin("unloadScript", "s", name)


def _kwin(member: str, signature: str, *args: str) -> None:
    subprocess.run(
        [
            "busctl",
            "--user",
            "call",
            "org.kde.KWin",
            "/Scripting",
            "org.kde.kwin.Scripting",
            member,
            signature,
            *args,
        ],
        check=True,
        capture_output=True,
    )


REPORT_JS = """
function report(s) {
    callDBus("org.yada.E2E", "/", "org.yada.E2E", "Report", s);
}
"""


@pytest.fixture
async def target(tmp_path, kwin_script, reports):
    """A focused text field in its own process, recording whatever lands in it."""
    out = tmp_path / "target.txt"
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "target_app.py"), str(out)],
        env=os.environ.copy(),
    )
    kwin_script(
        "e2e-focus-target",
        REPORT_JS
        + f"""
const TITLE = {TARGET_TITLE!r};
function grab(w) {{
    if (w && w.caption === TITLE) {{ workspace.activeWindow = w; report("target-focused"); }}
}}
workspace.windowAdded.connect(grab);
workspace.windowList().forEach(grab);
""",
    )
    await reports.wait_for(lambda line: line == "target-focused")
    yield out
    proc.terminate()
    proc.wait(timeout=5)


def run_yada(code: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a snippet against yada's real modules in a fresh process, as the app would."""
    return subprocess.run(
        [sys.executable, "-c", code],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def preauthorize_remote_desktop() -> None:
    """KDE's portal skips its consent dialog for apps in the kde-authorized table."""
    subprocess.run(
        [
            "busctl",
            "--user",
            "call",
            "org.freedesktop.impl.portal.PermissionStore",
            "/org/freedesktop/impl/portal/PermissionStore",
            "org.freedesktop.impl.portal.PermissionStore",
            "SetPermission",
            "sbssas",
            "kde-authorized",
            "true",
            "remote-desktop",
            "yada",
            "1",
            "yes",
        ],
        check=True,
        capture_output=True,
    )
