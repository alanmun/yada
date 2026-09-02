"""Wayland global hotkey via the XDG desktop portal's GlobalShortcuts interface.

On Wayland a client cannot grab keys -- that is a security property of the protocol, not a
gap. The portal is the sanctioned route: the app asks the compositor to own the binding, the
user confirms once in a KDE dialog, and thereafter the compositor delivers an `Activated`
signal. KDE Plasma implements this well; GNOME's support is patchier, which is why the
external backend exists as a fallback.

What the user sees: the first time yada starts, Plasma shows a dialog asking to allow the
shortcut. Approve it once and it is remembered. If they decline, or the portal is missing,
yada falls back to asking them to bind `yada toggle` in System Settings by hand.

The D-Bus request/response dance is fiddly and worth naming: portal methods return a
*Request* object path and deliver the real answer later as a `Response` signal on that path.
The path is predictable from the handle token, so this subscribes *before* calling to avoid
losing a fast reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil

from .base import Combo, TriggerCallback

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
GLOBAL_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_IFACE = "org.freedesktop.portal.Request"

SHORTCUT_ID = "toggle-recording"
# Shown in the compositor's own shortcut settings, so it should read as a user-facing name.
SHORTCUT_DESCRIPTION = "Start or stop dictation"

RESPONSE_TIMEOUT = 120.0  # the user has to click a dialog; do not rush them


def _request_path(unique_name: str, token: str) -> str:
    """Where the portal will emit the Response signal for a given handle token."""
    sender = unique_name.lstrip(":").replace(".", "_")
    return f"{PORTAL_PATH}/request/{sender}/{token}"


def _token() -> str:
    return "yada" + secrets.token_hex(8)


def wayland_session() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


class KdePortalHotkeyBackend:
    """Registers the shortcut with the compositor. Runs on the app's asyncio loop."""

    name = "kde_portal"

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._bus = None
        self._shortcuts = None
        self._session_handle: str | None = None
        self._on_trigger: TriggerCallback | None = None
        self._combo: Combo | None = None
        self._error: str | None = None
        self._bound = False
        self._task: asyncio.Task | None = None

    @staticmethod
    def available() -> bool:
        """Cheap pre-flight. The real test is whether BindShortcuts succeeds."""
        try:
            import dbus_fast  # noqa: F401
        except ImportError:
            return False
        if os.name == "nt":
            return False
        # A session bus must exist; without one there is no portal to talk to.
        return bool(
            os.environ.get("DBUS_SESSION_BUS_ADDRESS")
            or os.path.exists(f"/run/user/{os.getuid()}/bus")
        )

    # -- lifecycle ----------------------------------------------------------------------

    def start(self, combo: Combo, on_trigger: TriggerCallback) -> None:
        self._combo = combo
        self._on_trigger = on_trigger
        self._error = None
        self._bound = False
        # Scheduled rather than awaited: start() must not block the UI while the user
        # decides whether to approve a permission dialog.
        self._task = asyncio.run_coroutine_threadsafe(self._setup(), self._loop)  # type: ignore[assignment]

    def stop(self) -> None:
        if self._bus is not None:
            fut = asyncio.run_coroutine_threadsafe(self._teardown(), self._loop)
            with contextlib.suppress(Exception):
                fut.result(timeout=3.0)

    def problem(self) -> str | None:
        return self._error

    def status(self) -> str:
        combo = self._combo.display if self._combo else "shortcut"
        if self._error:
            return f"Portal shortcut unavailable: {self._error}"
        if self._bound:
            return (
                f"{combo} is registered with the compositor. "
                "You can review it in System Settings → Shortcuts."
            )
        return "Waiting for the desktop to confirm the shortcut…"

    @property
    def bound(self) -> bool:
        return self._bound

    @property
    def error(self) -> str | None:
        return self._error

    # -- portal plumbing ----------------------------------------------------------------

    async def _setup(self) -> None:
        try:
            from dbus_fast import Message, Variant
            from dbus_fast.aio import MessageBus
            from dbus_fast.constants import BusType
        except ImportError as exc:
            self._error = f"dbus-fast is not installed ({exc})"
            return

        try:
            self._bus = await MessageBus(bus_type=BusType.SESSION).connect()
            intro = await self._bus.introspect(PORTAL_BUS, PORTAL_PATH)
            obj = self._bus.get_proxy_object(PORTAL_BUS, PORTAL_PATH, intro)
            self._shortcuts = obj.get_interface(GLOBAL_SHORTCUTS)
        except Exception as exc:  # noqa: BLE001 - portal absent, old, or bus unreachable
            self._error = f"could not reach the desktop portal ({type(exc).__name__})"
            return

        # Match Response signals once, for every request this backend makes.
        with contextlib.suppress(Exception):
            await self._bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[f"type='signal',interface='{REQUEST_IFACE}',member='Response'"],
                )
            )

        try:
            session = await self._create_session(Variant)
        except Exception as exc:  # noqa: BLE001
            self._error = f"CreateSession failed ({exc})"
            return
        if session is None:
            self._error = "the desktop did not return a shortcuts session"
            return
        self._session_handle = session

        # Activated arrives every time the user presses the combo.
        self._shortcuts.on_activated(self._on_activated)

        try:
            await self._bind(Variant)
        except Exception as exc:  # noqa: BLE001
            self._error = f"BindShortcuts failed ({exc})"

    async def _create_session(self, Variant) -> str | None:
        token, session_token = _token(), _token()
        waiter = self._await_response(token)
        await self._shortcuts.call_create_session(
            {
                "handle_token": Variant("s", token),
                "session_handle_token": Variant("s", session_token),
            }
        )
        code, results = await waiter
        if code != 0:
            return None
        handle = results.get("session_handle")
        value = getattr(handle, "value", handle)
        return str(value) if value else None

    async def _bind(self, Variant) -> None:
        assert self._combo is not None
        token = _token()
        waiter = self._await_response(token)
        await self._shortcuts.call_bind_shortcuts(
            self._session_handle,
            [
                (
                    SHORTCUT_ID,
                    {
                        "description": Variant("s", SHORTCUT_DESCRIPTION),
                        # A *preferred* trigger: the compositor may assign something else,
                        # or the user may change it. yada follows whatever it ends up as,
                        # because the compositor is the source of truth once bound.
                        "preferred_trigger": Variant("s", self._combo.to_xdg()),
                    },
                )
            ],
            "",  # parent_window: none, this is a tray app with no window up yet
            {"handle_token": Variant("s", token)},
        )
        code, _ = await waiter
        if code == 0:
            self._bound = True
        elif code == 1:
            self._error = "the shortcut request was declined"
        else:
            self._error = f"the desktop rejected the shortcut (code {code})"

    def _await_response(self, token: str) -> asyncio.Future:
        """Subscribe to the Response for `token` before the call that triggers it."""
        from dbus_fast import MessageType

        assert self._bus is not None
        path = _request_path(self._bus.unique_name, token)
        future: asyncio.Future = self._loop.create_future()

        def handler(msg) -> bool | None:
            if (
                msg.message_type is not MessageType.SIGNAL
                or msg.interface != REQUEST_IFACE
                or msg.member != "Response"
                or msg.path != path
            ):
                return None
            if not future.done():
                code = int(msg.body[0]) if msg.body else 2
                raw = msg.body[1] if len(msg.body) > 1 else {}
                future.set_result((code, raw or {}))
            self._bus.remove_message_handler(handler)
            return True  # consumed

        self._bus.add_message_handler(handler)

        async def wait():
            try:
                return await asyncio.wait_for(future, timeout=RESPONSE_TIMEOUT)
            except TimeoutError:
                with contextlib.suppress(Exception):
                    self._bus.remove_message_handler(handler)
                return (2, {})

        return asyncio.ensure_future(wait())

    def _on_activated(self, session_handle, shortcut_id, _timestamp, _options) -> None:
        if shortcut_id != SHORTCUT_ID or session_handle != self._session_handle:
            return
        if self._on_trigger is not None:
            self._on_trigger()

    async def _teardown(self) -> None:
        bus, self._bus = self._bus, None
        self._shortcuts = None
        self._session_handle = None
        self._bound = False
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.disconnect()


def ydotool_available() -> bool:
    """Unrelated to hotkeys, but the same Wayland restriction governs pasting."""
    return shutil.which("ydotool") is not None
