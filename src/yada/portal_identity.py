"""Tell the XDG desktop portal which app is calling.

A host (non-Flatpak) app has no identity of its own on the portal. xdg-desktop-portal
guesses one from the caller's systemd scope, so yada launched from VS Code's terminal runs
inside `app-com.microsoft.VSCode-*.scope` and Plasma asks whether *Visual Studio Code* may
register yada's shortcut -- and stores the approval under VS Code's name.

`org.freedesktop.host.portal.Registry.Register` (xdg-desktop-portal 1.19+) fixes that by
naming the app explicitly. Two rules govern it:

* It is per D-Bus connection and must come before any other portal call on that
  connection; the portal pins the app id on first use.
* The id must match an installed desktop file. yada writes `yada.desktop` on launch from an
  installed build, so a source checkout has none and registration fails there.

Every failure -- an older portal without the interface, a missing desktop file, a sandbox
-- leaves the connection exactly as it was before, so callers fall back to the old guess.
"""

from __future__ import annotations

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REGISTRY_IFACE = "org.freedesktop.host.portal.Registry"

# The basename of the desktop file YadaApp._sync_linux_integration writes.
APP_ID = "yada"


async def register_app_id(bus) -> str | None:
    """Claim yada's app id for `bus`. Returns None on success, else why it did not.

    Never raises: a portal that cannot be told who we are is still worth using.
    """
    try:
        from dbus_fast import Message, MessageType

        reply = await bus.call(
            Message(
                destination=PORTAL_BUS,
                path=PORTAL_PATH,
                interface=REGISTRY_IFACE,
                member="Register",
                signature="sa{sv}",
                body=[APP_ID, {}],
            )
        )
    except Exception as exc:  # noqa: BLE001 - bus gone or dbus-fast missing; fall back
        return f"{type(exc).__name__}: {exc}"
    if reply is None:
        return "no reply from the portal"
    if reply.message_type is MessageType.ERROR:
        detail = reply.body[0] if reply.body else ""
        return f"{reply.error_name}: {detail}".rstrip(": ")
    return None
