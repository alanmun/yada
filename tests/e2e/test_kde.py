"""yada against a real KWin. Each test names the bug it would have caught."""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from .conftest import REPORT_JS, preauthorize_remote_desktop, run_yada


async def _wait_for_file(path: Path, expected: str, timeout: float = 10.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    text = ""
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text == expected:
                return text
        await asyncio.sleep(0.1)
    return text


def test_clipboard_lands_without_focus():
    """0.1.37: Qt's write was dropped (no focus) yet read back as if it had worked."""
    text = "yada e2e clipboard check"
    proc = run_yada(
        "import sys\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication(sys.argv)\n"
        "from yada.output.clipboard import copy\n"
        f"print(copy({text!r}))\n"
    )
    assert "(True, None)" in proc.stdout, proc.stderr[-2000:]
    held = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True)
    assert held.stdout == text, "the compositor does not hold what yada said it copied"


async def test_overlay_sits_bottom_centre_and_never_keeps_focus(target, reports, kwin_script):
    """0.1.33: centred mid-screen. 0.1.37: KWin focused it, so Ctrl+V landed in the overlay."""
    kwin_script(
        "e2e-watch",
        REPORT_JS
        + """
workspace.windowActivated.connect(function (w) {
    if (w) report("active " + w.caption);
});
workspace.windowAdded.connect(function (w) {
    if (w.resourceClass !== "yada") return;
    // Do what KWin did on a real desktop, but every time: focus the overlay as it
    // appears. yada's own script must hand focus straight back.
    workspace.activeWindow = w;
    w.closed.connect(function () { report("overlay-closed"); });
    const area = workspace.clientArea(KWin.PlacementArea, w.output, workspace.currentDesktop);
    w.frameGeometryChanged.connect(function () {
        const g = w.frameGeometry;
        report("overlay " + Math.round(g.x) + " " + Math.round(g.y) + " " + Math.round(g.width)
            + " " + Math.round(g.height) + " area " + area.x + " " + area.y + " "
            + area.width + " " + area.height);
    });
});
""",
    )
    mark = len(reports.lines)
    proc = await asyncio.to_thread(
        run_yada,
        "import asyncio, sys\n"
        "from PySide6.QtCore import QTimer\n"
        "from PySide6.QtWidgets import QApplication\n"
        "from yada.ui import kwin_placement\n"
        "from yada.ui.overlay import LiveOverlay\n"
        "assert asyncio.run(kwin_placement.install()) is None\n"
        "app = QApplication(sys.argv); app.setDesktopFileName('yada')\n"
        "o = LiveOverlay(); o.set_enabled(True); o.begin()\n"
        "QTimer.singleShot(800, lambda: o.set_partial('a longer line of live text ' * 3))\n"
        "QTimer.singleShot(2500, app.quit)\n"
        "app.exec()\n"
        "asyncio.run(kwin_placement.uninstall())\n",
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    placed = [line for line in reports.since(mark) if line.startswith("overlay ")]
    assert placed, f"KWin never reported the overlay; reports: {reports.since(mark)}"
    x, y, w, h, _ax, ay, aw, ah = (
        int(n) for n in re.findall(r"-?\d+", placed[-1].replace("area", ""))
    )
    assert abs((x + w / 2) - aw / 2) <= 2, f"not horizontally centred: {placed[-1]}"
    assert y + h > ay + ah * 0.75, f"not near the bottom: {placed[-1]}"

    # What matters is focus while the overlay is still up -- that is when the paste
    # happens. Once it closes, KWin refocuses the target regardless.
    events = [
        line
        for line in reports.since(mark)
        if line.startswith("active ") or line == "overlay-closed"
    ]
    assert "active yada live overlay" in events, f"the test never focused the overlay: {events}"
    assert "overlay-closed" in events, f"the overlay never closed: {events}"
    while_shown = events[: events.index("overlay-closed")]
    last = len(while_shown) - 1 - while_shown[::-1].index("active yada live overlay")
    assert "active yada e2e target" in while_shown[last + 1 :], (
        f"focus stayed on the overlay while it was shown: {events}"
    )


async def test_transcript_is_pasted_into_the_focused_app(target):
    """0.1.37: the old clipboard got pasted, or the keystroke went into the overlay."""
    preauthorize_remote_desktop()
    text = "Dictated words should land here."
    proc = await asyncio.to_thread(
        run_yada,
        "import sys, threading, time\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication(sys.argv)\n"
        "from yada.output.clipboard import copy\n"
        "from yada.output.portal_paste import PortalPasteBackend\n"
        "backend = PortalPasteBackend()\n"
        "declined = threading.Event()\n"
        "backend.request_permission(declined.set)\n"
        "for _ in range(200):\n"
        "    s = backend._session\n"
        "    if (s is not None and s.ready) or declined.is_set() or backend._error:\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "print('ready', backend._session is not None and backend._session.ready, backend._error)\n"
        f"print('copy', copy({text!r}))\n"
        "print('paste', backend.paste())\n",
        timeout=40,
    )
    assert "ready True" in proc.stdout, proc.stdout + proc.stderr[-2000:]
    landed = await _wait_for_file(target, text)
    assert landed == text, f"the target received {landed!r}; yada said: {proc.stdout}"
