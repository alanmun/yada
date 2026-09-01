"""The command channel that every trigger source funnels through."""

from __future__ import annotations

import threading

import pytest

from yada import ipc


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("YADA_RUNTIME_DIR", str(tmp_path / "run"))
    return tmp_path


def test_no_server_means_no_response(runtime):
    assert ipc.send_command("toggle") is None
    assert ipc.is_running() is False


def test_command_round_trip(runtime):
    seen: list[tuple[str, dict]] = []

    def handler(cmd, payload):
        seen.append((cmd, payload))
        return {"ok": True, "state": "recording"}

    server = ipc.CommandServer(handler)
    server.start()
    try:
        assert ipc.is_running() is True
        assert ipc.send_command("toggle", {"source": "hotkey"}) == {
            "ok": True,
            "state": "recording",
        }
        assert seen == [("toggle", {"source": "hotkey"})]
    finally:
        server.stop()


def test_ping_is_handled_without_the_app_handler(runtime):
    def handler(cmd, payload):
        raise AssertionError("ping must not reach the app handler")

    server = ipc.CommandServer(handler)
    server.start()
    try:
        assert ipc.send_command("ping") == {"ok": True}
    finally:
        server.stop()


def test_second_instance_is_rejected(runtime):
    server = ipc.CommandServer(lambda c, p: {"ok": True})
    server.start()
    try:
        with pytest.raises(ipc.AlreadyRunning):
            ipc.CommandServer(lambda c, p: {"ok": True}).start()
    finally:
        server.stop()


def test_stale_socket_does_not_block_startup(runtime):
    """A crashed instance leaves a socket file behind; the next launch must still bind."""
    path = ipc.socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a socket")
    assert ipc.is_running() is False
    server = ipc.CommandServer(lambda c, p: {"ok": True})
    server.start()
    try:
        assert ipc.send_command("ping") == {"ok": True}
    finally:
        server.stop()


def test_handler_exception_is_contained(runtime):
    def handler(cmd, payload):
        raise ValueError("boom")

    server = ipc.CommandServer(handler)
    server.start()
    try:
        reply = ipc.send_command("toggle")
        assert reply is not None and reply["ok"] is False and "boom" in reply["error"]
        # Server must still be alive for the next keypress.
        assert ipc.send_command("ping") == {"ok": True}
    finally:
        server.stop()


def test_concurrent_commands(runtime):
    calls: list[str] = []
    lock = threading.Lock()

    def handler(cmd, payload):
        with lock:
            calls.append(cmd)
        return {"ok": True}

    server = ipc.CommandServer(handler)
    server.start()
    try:
        threads = [
            threading.Thread(target=lambda i=i: ipc.send_command(f"cmd{i}")) for i in range(12)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(calls) == 12
    finally:
        server.stop()


def test_stop_releases_the_socket(runtime):
    server = ipc.CommandServer(lambda c, p: {"ok": True})
    server.start()
    server.stop()
    assert ipc.is_running() is False
    # And a fresh instance can take over.
    again = ipc.CommandServer(lambda c, p: {"ok": True})
    again.start()
    try:
        assert ipc.is_running() is True
    finally:
        again.stop()
