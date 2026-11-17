"""Telling the desktop portal that the caller is yada, not the app that launched it.

Without this, Plasma attributes yada's shortcut to whatever started it -- run from VS Code's
terminal, the dialog asks whether *Visual Studio Code* may register "Start or stop
dictation". Registration must never be fatal: older portals lack the interface.
"""

from __future__ import annotations

import pytest

dbus_fast = pytest.importorskip("dbus_fast")
from dbus_fast import Message, MessageType  # noqa: E402

from yada.portal_identity import APP_ID, REGISTRY_IFACE, register_app_id  # noqa: E402


class FakeBus:
    def __init__(self, reply=None, raises=None):
        self.reply = reply
        self.raises = raises
        self.sent: list[Message] = []

    async def call(self, msg):
        self.sent.append(msg)
        if self.raises:
            raise self.raises
        return self.reply


async def test_registers_yada_as_the_desktop_file_id():
    bus = FakeBus(reply=Message(message_type=MessageType.METHOD_RETURN, reply_serial=1))
    assert await register_app_id(bus) is None
    (msg,) = bus.sent
    assert (msg.interface, msg.member) == (REGISTRY_IFACE, "Register")
    assert msg.body == [APP_ID, {}]
    assert APP_ID == "yada"  # must match the yada.desktop that app.py installs


async def test_old_portal_without_registry_falls_back():
    reply = Message(
        message_type=MessageType.ERROR,
        error_name="org.freedesktop.DBus.Error.UnknownMethod",
        reply_serial=1,
        signature="s",
        body=["No such interface"],
    )
    problem = await register_app_id(FakeBus(reply=reply))
    assert problem == "org.freedesktop.DBus.Error.UnknownMethod: No such interface"


async def test_missing_desktop_file_falls_back():
    reply = Message(
        message_type=MessageType.ERROR,
        error_name="org.freedesktop.portal.Error.Failed",
        reply_serial=1,
        signature="s",
        body=["Could not register app ID: App info not found for 'yada'"],
    )
    assert "App info not found" in await register_app_id(FakeBus(reply=reply))


async def test_bus_failure_does_not_raise():
    problem = await register_app_id(FakeBus(raises=ConnectionError("bus closed")))
    assert problem == "ConnectionError: bus closed"
