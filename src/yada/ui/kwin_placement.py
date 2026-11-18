"""Bottom-centre the live overlay on KDE Wayland, where yada cannot place it itself.

Wayland gives clients no say over where their windows go: `QWidget.move()` is silently
ignored, so the overlay landed wherever KWin's placement policy put it -- the middle of
the screen, on top of the text being dictated into. Only the compositor can position a
window, and KWin lets a script do it.

So on a KDE Wayland session yada loads a small KWin script over D-Bus. It watches for
windows of class `yada` titled `OVERLAY_TITLE`, pins them bottom-centre of the active
screen's work area (the same spot `LiveOverlay._reposition` computes on X11 and Windows),
keeps them above other windows, re-centres them as the text grows, and hands keyboard
focus back to the previous window if KWin activates the overlay -- otherwise yada's Ctrl+V
lands in the overlay instead of where the user was typing.

The script lives in KWin, not in yada, so it is unloaded on quit and replaced on start --
a crash leaves at most a script that matches nothing until the next launch. Anything
going wrong here costs only placement, never startup.

Two KWin details worth knowing: a plain (non-declarative) script has no `Qt` global, so
rectangles are object literals; and `print()` output is filtered from the journal by
default, so a misbehaving script is easiest to debug with `callDBus` to the bus daemon.
"""

from __future__ import annotations

import os
from pathlib import Path

KWIN_BUS = "org.kde.KWin"
SCRIPTING_PATH = "/Scripting"
SCRIPTING_IFACE = "org.kde.kwin.Scripting"
PLUGIN_NAME = "yada-overlay-placement"

# Matched by the script; the overlay sets this as its window title.
OVERLAY_TITLE = "yada live overlay"

SCRIPT = """
const TITLE = %(title)s;
function isOverlay(w) {
    return w && w.resourceClass === "yada" && w.caption === TITLE;
}
function place(w) {
    const area = workspace.clientArea(
        KWin.PlacementArea, workspace.activeScreen, workspace.currentDesktop);
    const g = w.frameGeometry;
    const margin = Math.max(24, Math.round(area.height * 0.06));
    const x = Math.round(area.x + (area.width - g.width) / 2);
    const y = Math.round(area.y + area.height - g.height - margin);
    if (Math.round(g.x) !== x || Math.round(g.y) !== y)
        w.frameGeometry = {x: x, y: y, width: g.width, height: g.height};
}
function track(w) {
    if (!isOverlay(w)) return;
    w.keepAbove = true;
    w.skipTaskbar = true;
    w.skipSwitcher = true;
    w.skipPager = true;
    place(w);
    w.frameGeometryChanged.connect(function () { place(w); });
}
workspace.windowAdded.connect(track);
workspace.windowList().forEach(track);

// The overlay must never hold keyboard focus: yada pastes with Ctrl+V into whatever has
// it. Qt's WA_ShowWithoutActivating means nothing on Wayland, and KWin sometimes activates
// the overlay as it appears -- the paste then lands in the overlay and vanishes. Hand
// focus straight back to the window that had it.
let lastFocused = isOverlay(workspace.activeWindow) ? null : workspace.activeWindow;
workspace.windowActivated.connect(function (w) {
    if (!w) return;
    if (!isOverlay(w)) {
        lastFocused = w;
        return;
    }
    if (lastFocused && !lastFocused.deleted) workspace.activeWindow = lastFocused;
});
"""


def kde_wayland_session() -> bool:
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" and "KDE" in desktops


def script_source() -> str:
    import json

    return SCRIPT % {"title": json.dumps(OVERLAY_TITLE)}


def _script_path() -> Path:
    # KWin reads the file by path, so it must be somewhere it can see: the runtime dir is
    # per-user tmpfs, and the data dir is the fallback yada already writes its icon to.
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "yada" / "kwin-overlay-placement.js"


async def _call(bus, member: str, signature: str, body: list):
    from dbus_fast import Message, MessageType

    reply = await bus.call(
        Message(
            destination=KWIN_BUS,
            path=SCRIPTING_PATH,
            interface=SCRIPTING_IFACE,
            member=member,
            signature=signature,
            body=body,
        )
    )
    if reply is None or reply.message_type is MessageType.ERROR:
        detail = reply.body[0] if reply is not None and reply.body else ""
        raise RuntimeError(f"{member}: {getattr(reply, 'error_name', 'no reply')} {detail}")
    return reply.body[0] if reply.body else None


async def install() -> str | None:
    """Load the placement script into KWin. Returns None on success, else why not."""
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType
    except ImportError as exc:
        return f"dbus-fast is not installed ({exc})"

    path = _script_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script_source(), encoding="utf-8")
    except OSError as exc:
        return f"could not write the KWin script ({exc})"

    bus = None
    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        # A previous run that crashed, or an older yada's script, would otherwise keep
        # loadScript from replacing it.
        await _call(bus, "unloadScript", "s", [PLUGIN_NAME])
        script_id = await _call(bus, "loadScript", "ss", [str(path), PLUGIN_NAME])
        if script_id is None or int(script_id) < 0:
            return "KWin refused the placement script"
        await _call(bus, "start", "", [])
        return None
    except Exception as exc:  # noqa: BLE001 - not KDE, old KWin, or bus gone
        return f"{type(exc).__name__}: {exc}"
    finally:
        if bus is not None:
            bus.disconnect()


async def uninstall() -> None:
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType
    except ImportError:
        return
    bus = None
    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        await _call(bus, "unloadScript", "s", [PLUGIN_NAME])
    except Exception:  # noqa: BLE001 - quitting anyway
        pass
    finally:
        if bus is not None:
            bus.disconnect()
