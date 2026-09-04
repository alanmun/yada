"""Pasting on Wayland through the RemoteDesktop portal.

Wayland does not let one application press keys in another, and that is deliberate rather
than an oversight. The sanctioned way to ask anyway is
`org.freedesktop.portal.RemoteDesktop`: the compositor prompts the user once, and after
that yada may synthesise keys *through* the portal instead of around it.

That removes the ydotool requirement entirely -- no daemon, no `/dev/uinput`, no adding
yourself to the `input` group -- at the cost of one approval dialog. The portal's restore
token is kept so the dialog does not come back on every launch.

Two deliberate choices, both about not freezing the app:

* The session is established in the background. `paste()` never waits for a consent dialog;
  if the session is not ready it says so and the text is already on the clipboard, and the
  next paste works. Blocking the Qt thread on a modal system dialog would hang the window.
* Every portal call has a timeout. A portal that never answers must cost one message, not
  a wedged application.

Unverified against a real compositor at the time of writing. It is written to fail closed:
any failure is reported and the clipboard still has the text.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import threading

from ..config import config_dir

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# SelectDevices `types` is a bitmask; keyboard only.
DEVICE_KEYBOARD = 1
# persist_mode 2 == "persist until explicitly revoked", which is what makes the consent
# dialog a one-off rather than a thing the user sees on every launch.
PERSIST_UNTIL_REVOKED = 2

KEYSYM_CONTROL_L = 0xFFE3
KEYSYM_V = 0x0076
RELEASED, PRESSED = 0, 1

# Long enough for a person to notice a dialog and click it; short enough that a portal
# which never answers does not leave the session pending for ever.
CONSENT_TIMEOUT = 120.0
CALL_TIMEOUT = 5.0


def _token() -> str:
    return "yada" + secrets.token_hex(8)


def _request_path(unique_name: str, token: str) -> str:
    sender = unique_name.lstrip(":").replace(".", "_")
    return f"{PORTAL_PATH}/request/{sender}/{token}"


def wayland_session() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or (
        os.environ.get("XDG_SESSION_TYPE") == "wayland"
    )


def _token_file():
    return config_dir() / "remote-desktop.token"


def _read_restore_token() -> str:
    try:
        return _token_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_restore_token(token: str) -> None:
    if not token:
        return
    path = _token_file()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        path.chmod(0o600)


class PortalPasteBackend:
    """Sends Ctrl+V through the RemoteDesktop portal."""

    name = "portal"

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: _PortalSession | None = None
        self._starting = False
        self._error: str | None = None

    @staticmethod
    def available() -> bool:
        if not wayland_session():
            return False
        try:
            import dbus_fast  # noqa: F401
        except ImportError:
            return False
        return True

    # -- the worker loop ----------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Its own loop, so this works from the Qt thread, doctor and the settings pane.

        The app has an asyncio loop of its own, but the paste backend is built in four
        places that do not all have access to it, and threading one through each of them to
        press Ctrl+V is not worth the coupling.
        """
        if self._loop is not None:
            return self._loop
        ready = threading.Event()
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        self._thread = threading.Thread(target=run, name="yada-portal", daemon=True)
        self._thread.start()
        ready.wait(timeout=5.0)
        self._loop = loop
        return loop

    # -- PasteBackend -------------------------------------------------------------------

    def paste(self) -> tuple[bool, str | None]:
        session = self._session
        if session is not None and session.ready:
            future = asyncio.run_coroutine_threadsafe(session.send_paste(), self._ensure_loop())
            try:
                return future.result(timeout=CALL_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - a failed paste must not raise upward
                return False, f"The portal refused the keystroke ({type(exc).__name__})."

        self._begin_session()
        if self._error:
            return False, self._error
        return False, (
            "Waiting for permission to send keystrokes — approve the desktop's prompt "
            "once and pasting will work from then on. The text is on your clipboard."
        )

    def _begin_session(self) -> None:
        """Start establishing the session, at most once, without blocking the caller."""
        if self._starting or (self._session is not None and self._session.ready):
            return
        self._starting = True
        session = _PortalSession()
        self._session = session

        async def establish() -> None:
            try:
                await session.establish()
            except Exception as exc:  # noqa: BLE001
                self._error = f"Could not set up the desktop portal ({type(exc).__name__})."
            finally:
                self._starting = False
                if session.error:
                    self._error = session.error

        asyncio.run_coroutine_threadsafe(establish(), self._ensure_loop())

    def describe(self) -> str:
        if self._session is not None and self._session.ready:
            return "Pastes by sending Ctrl+V through the desktop's RemoteDesktop portal."
        return (
            "Pastes through the desktop's RemoteDesktop portal, which needs nothing "
            "installed. Your desktop will ask for permission the first time; approve it "
            "once and pasting works from then on."
        )


class _PortalSession:
    """One RemoteDesktop session: create, select a keyboard, start, then notify keys."""

    def __init__(self) -> None:
        self._bus = None
        self._iface = None
        self._handle: str | None = None
        self.ready = False
        self.error: str | None = None

    async def establish(self) -> None:
        try:
            from dbus_fast import Message, Variant
            from dbus_fast.aio import MessageBus
            from dbus_fast.constants import BusType
        except ImportError as exc:
            self.error = f"dbus-fast is not installed ({exc})"
            return

        try:
            self._bus = await MessageBus(bus_type=BusType.SESSION).connect()
            intro = await self._bus.introspect(PORTAL_BUS, PORTAL_PATH)
            obj = self._bus.get_proxy_object(PORTAL_BUS, PORTAL_PATH, intro)
            self._iface = obj.get_interface(REMOTE_DESKTOP)
        except Exception as exc:  # noqa: BLE001 - portal absent, old, or bus unreachable
            self.error = (
                "This desktop does not offer the RemoteDesktop portal "
                f"({type(exc).__name__})."
            )
            return

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

        handle = await self._create_session(Variant)
        if handle is None:
            self.error = self.error or "The desktop did not return a session."
            return
        self._handle = handle

        if not await self._select_keyboard(Variant):
            return
        if not await self._start(Variant):
            return
        self.ready = True

    async def _create_session(self, Variant) -> str | None:
        token, session_token = _token(), _token()
        waiter = self._await_response(token, CALL_TIMEOUT)
        await self._iface.call_create_session(
            {
                "handle_token": Variant("s", token),
                "session_handle_token": Variant("s", session_token),
            }
        )
        code, results = await waiter
        if code != 0:
            self.error = "The desktop declined to create a session."
            return None
        handle = results.get("session_handle")
        value = getattr(handle, "value", handle)
        return str(value) if value else None

    async def _select_keyboard(self, Variant) -> bool:
        token = _token()
        options = {
            "handle_token": Variant("s", token),
            "types": Variant("u", DEVICE_KEYBOARD),
            "persist_mode": Variant("u", PERSIST_UNTIL_REVOKED),
        }
        if restore := _read_restore_token():
            options["restore_token"] = Variant("s", restore)
        waiter = self._await_response(token, CALL_TIMEOUT)
        await self._iface.call_select_devices(self._handle, options)
        code, _results = await waiter
        if code != 0:
            self.error = "The desktop declined the keyboard request."
            return False
        return True

    async def _start(self, Variant) -> bool:
        token = _token()
        # The consent dialog appears here, so this is the one call that waits on a person.
        waiter = self._await_response(token, CONSENT_TIMEOUT)
        await self._iface.call_start(self._handle, "", {"handle_token": Variant("s", token)})
        code, results = await waiter
        if code != 0:
            self.error = (
                "Permission to send keystrokes was declined. Auto-paste stays off; the "
                "text is still copied to your clipboard."
            )
            return False
        restore = results.get("restore_token")
        _write_restore_token(str(getattr(restore, "value", restore) or ""))
        return True

    async def send_paste(self) -> tuple[bool, str | None]:
        if not self.ready or self._iface is None or self._handle is None:
            return False, "The portal session is not ready."
        for keysym, state in (
            (KEYSYM_CONTROL_L, PRESSED),
            (KEYSYM_V, PRESSED),
            (KEYSYM_V, RELEASED),
            (KEYSYM_CONTROL_L, RELEASED),
        ):
            await self._iface.call_notify_keyboard_keysym(self._handle, {}, keysym, state)
        return True, None

    def _await_response(self, token: str, timeout: float) -> asyncio.Future:
        """Resolve when the portal answers this request, or time out."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        path = _request_path(self._bus.unique_name, token)

        def handler(msg) -> bool | None:
            if (
                msg.message_type.name != "SIGNAL"
                or msg.interface != REQUEST_IFACE
                or msg.member != "Response"
                or msg.path != path
            ):
                return None
            if not future.done():
                code = msg.body[0]
                results = msg.body[1] if len(msg.body) > 1 else {}
                future.set_result((code, results))
            return True

        self._bus.add_message_handler(handler)

        async def wait():
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError:
                return 2, {}
            finally:
                with contextlib.suppress(Exception):
                    self._bus.remove_message_handler(handler)

        return asyncio.ensure_future(wait())
