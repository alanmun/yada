"""Single-instance guard and a local command channel.

Every way of triggering a recording funnels through one internal command, so the app never
cares where the trigger came from:

    Windows hotkey (RegisterHotKey)  ─┐
    KDE GlobalShortcuts portal       ─┼─→ "toggle" ─→ Session state machine
    tray icon click                  ─┤
    `yada toggle` from a KDE binding ─┘

That last one is why this module exists. A Wayland client cannot grab keys, so on KDE the
reliable path is binding `Ctrl+Shift+;` in System Settings to `yada toggle`, which pokes the
already-running instance. That command runs on every keypress, so it is stdlib-only and
imports nothing heavy -- pulling in Qt to send four bytes would add a visible delay to the
one action that must feel instant.

Transport is a Unix domain socket on Linux (filesystem permissions, no port) and a loopback
TCP socket with a shared token on Windows, which has no AF_UNIX in practice.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets as _secrets
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path

# Deliberately short. If the app is wedged, the hotkey should fall through quickly rather
# than leave the user pressing keys at a hung process.
CONNECT_TIMEOUT = 1.5
IO_TIMEOUT = 3.0
MAX_MESSAGE = 64 * 1024

CommandHandler = Callable[[str, dict], dict]


def runtime_dir() -> Path:
    """Somewhere per-user and short-lived for the socket and token."""
    if override := os.environ.get("YADA_RUNTIME_DIR"):
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "yada" / "run"
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(xdg) / "yada"
    return Path(f"/tmp/yada-{os.getuid()}")


def socket_path() -> Path:
    return runtime_dir() / "yada.sock"


def endpoint_path() -> Path:
    """Windows only: holds the loopback port and auth token."""
    return runtime_dir() / "endpoint.json"


def _ensure_runtime_dir() -> Path:
    d = runtime_dir()
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError, NotImplementedError):
        d.chmod(0o700)
    return d


# --------------------------------------------------------------------------------------
# Client -- must stay fast and dependency-free
# --------------------------------------------------------------------------------------


def _connect() -> tuple[socket.socket, str | None] | None:
    """Connect to a running instance, or None if there isn't one."""
    if sys.platform == "win32":
        try:
            info = json.loads(endpoint_path().read_text(encoding="utf-8"))
            port, token = int(info["port"]), str(info["token"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            sock.close()
            return None
        return sock, token

    path = socket_path()
    if not path.exists():
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect(str(path))
    except OSError:
        # A stale socket file from a crashed instance. Remove it so the next launch can
        # bind cleanly instead of refusing to start.
        sock.close()
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return sock, None


def send_command(command: str, payload: dict | None = None) -> dict | None:
    """Send a command to the running instance. None means nothing was listening."""
    conn = _connect()
    if conn is None:
        return None
    sock, token = conn
    message = {"cmd": command, "payload": payload or {}}
    if token:
        message["token"] = token
    try:
        sock.settimeout(IO_TIMEOUT)
        sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n") and len(data) < MAX_MESSAGE:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def is_running() -> bool:
    return send_command("ping") is not None


# --------------------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------------------


class AlreadyRunning(RuntimeError):
    """Another instance holds the socket. The caller should forward its command and exit."""


class CommandServer:
    """Accepts commands from other processes and dispatches them on a worker thread.

    The handler is invoked off the Qt main thread, so implementations must marshal back
    with a Qt signal rather than touching widgets directly.
    """

    def __init__(self, handler: CommandHandler) -> None:
        self._handler = handler
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._token: str | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if is_running():
            raise AlreadyRunning("another yada instance is already listening")
        _ensure_runtime_dir()
        self._sock = self._bind()
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._serve, name="yada-ipc", daemon=True)
        self._thread.start()

    def _bind(self) -> socket.socket:
        if sys.platform == "win32":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            self._token = _secrets.token_urlsafe(24)
            port = sock.getsockname()[1]
            # Token gates the loopback port: any local process can reach 127.0.0.1, so the
            # port alone is not an authorisation boundary.
            endpoint_path().write_text(
                json.dumps({"port": port, "token": self._token}), encoding="utf-8"
            )
            return sock

        path = socket_path()
        with contextlib.suppress(OSError):
            path.unlink()  # stale socket from a previous crash
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        with contextlib.suppress(OSError, NotImplementedError):
            path.chmod(0o600)
        return sock

    def stop(self) -> None:
        self._stopping.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        if sys.platform == "win32":
            with contextlib.suppress(OSError):
                endpoint_path().unlink()
        else:
            with contextlib.suppress(OSError):
                socket_path().unlink()

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stopping.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # socket closed during shutdown
            threading.Thread(
                target=self._handle, args=(conn,), name="yada-ipc-conn", daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(IO_TIMEOUT)
            data = b""
            while not data.endswith(b"\n") and len(data) < MAX_MESSAGE:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not data:
                return
            message = json.loads(data.decode("utf-8"))
            if self._token and message.get("token") != self._token:
                response = {"ok": False, "error": "unauthorised"}
            else:
                cmd = str(message.get("cmd", ""))
                payload = message.get("payload") or {}
                response = (
                    {"ok": True}
                    if cmd == "ping"
                    else self._handler(cmd, payload if isinstance(payload, dict) else {})
                )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the server
            response = {"ok": False, "error": str(exc)[:200]}
        with contextlib.suppress(OSError):
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        with contextlib.suppress(OSError):
            conn.close()
