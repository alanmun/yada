"""The tray icon: yada's primary interface.

There is no main window in the normal flow. The tray icon is the app, so it carries state
(via colour), the last result, and every action worth reaching without a keyboard.

Two things here are direct responses to how Whispering behaves:

* **Left-click toggles recording.** Whispering reserves left-click for exactly this and then
  never wires it up, so clicking its tray icon does nothing.
* **Nothing here quits the app implicitly.** Closing a settings window hides it. Only the
  explicit Quit action exits. See `app.py`, which sets `setQuitOnLastWindowClosed(False)` --
  its absence is why Whispering's window close terminates the app on Windows.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..pipeline.session import SessionResult, SessionState
from .icons import all_state_icons

STATE_LABELS: dict[SessionState, str] = {
    SessionState.IDLE: "Ready",
    SessionState.RECORDING: "Recording…",
    SessionState.TRANSCRIBING: "Transcribing…",
    SessionState.TRANSFORMING: "Cleaning up…",
}


class TrayIcon(QObject):
    """Wraps QSystemTrayIcon. Emits intent; the app decides what to do."""

    toggle_requested = Signal()
    settings_requested = Signal()
    copy_last_requested = Signal()
    check_updates_requested = Signal()
    restart_requested = Signal()
    quit_requested = Signal()

    def __init__(self, *, shortcut_label: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._icons: dict[SessionState, QIcon] = all_state_icons()
        self._state = SessionState.IDLE
        self._shortcut_label = shortcut_label
        self._last_text: str | None = None
        self._update_ready: str | None = None
        self._status_line = "Ready"
        # Set from settings once they are loaded; see app.py::_apply_notification_setting.
        # Defaults to on so a notification is never lost before the setting is read.
        self.notifications_enabled = True
        # The last thing that went wrong, kept on the tooltip. With notifications off --
        # the default on Windows -- this was the promise that warnings would still be
        # readable somewhere, and for a release it was not kept: every warning went to
        # `notify()` and nowhere else, so a degraded transcription explained itself to
        # nobody.
        self._problem: str | None = None

        self._tray = QSystemTrayIcon(self._icons[SessionState.IDLE])
        self._tray.activated.connect(self._on_activated)

        self._menu = QMenu()
        self._action_toggle = QAction("Start dictation", self._menu)
        self._action_toggle.triggered.connect(self.toggle_requested.emit)

        self._action_copy = QAction("Copy last result", self._menu)
        self._action_copy.setEnabled(False)
        self._action_copy.triggered.connect(self.copy_last_requested.emit)

        self._action_settings = QAction("Settings…", self._menu)
        self._action_settings.triggered.connect(self.settings_requested.emit)

        self._action_update = QAction("Check for updates", self._menu)
        self._action_update.triggered.connect(self.check_updates_requested.emit)

        # Only appears once an update is downloaded and waiting. Restarting is the entire
        # install step, so offering it at the moment it becomes useful -- and not before --
        # is clearer than a permanent action that usually does nothing interesting.
        self._action_restart = QAction("Restart to finish updating", self._menu)
        self._action_restart.triggered.connect(self.restart_requested.emit)
        self._action_restart.setVisible(False)

        self._action_quit = QAction("Quit yada", self._menu)
        self._action_quit.triggered.connect(self.quit_requested.emit)

        self._menu.addAction(self._action_toggle)
        self._menu.addSeparator()
        self._menu.addAction(self._action_copy)
        self._menu.addSeparator()
        self._menu.addAction(self._action_settings)
        self._menu.addAction(self._action_update)
        self._menu.addSeparator()
        self._menu.addAction(self._action_restart)
        self._menu.addAction(self._action_quit)
        self._tray.setContextMenu(self._menu)
        self._refresh()

    # -- lifecycle ----------------------------------------------------------------------

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    # -- state --------------------------------------------------------------------------

    @Slot(object)
    def set_state(self, state: SessionState) -> None:
        self._state = state
        self._tray.setIcon(self._icons[state])
        self._status_line = STATE_LABELS.get(state, str(state))
        self._refresh()

    def set_shortcut_label(self, label: str) -> None:
        self._shortcut_label = label
        self._refresh()

    def set_problem(self, message: str | None) -> None:
        self._problem = (message or "").strip() or None
        self._refresh()

    def set_result(self, result: SessionResult) -> None:
        self._last_text = result.final_text
        self._action_copy.setEnabled(bool(result.final_text))
        words = len(result.final_text.split())
        path = "streamed" if result.streamed else "uploaded"
        self._status_line = (
            f"{words} word{'s' if words != 1 else ''} in {result.duration_seconds:.1f}s ({path})"
        )
        self._refresh()

    def set_update_ready(self, version: str | None) -> None:
        """Reflected in the menu, not as a popup.

        An update that is already downloaded and staged needs no interruption -- it applies
        the next time yada starts. Saying so in the menu is enough.
        """
        self._update_ready = version
        self._action_update.setText(
            f"Version {version} is ready" if version else "Check for updates"
        )
        self._action_update.setEnabled(version is None)
        self._action_restart.setVisible(version is not None)
        if version:
            self._action_restart.setText(f"Restart to finish updating to {version}")
        self._refresh()

    @property
    def last_text(self) -> str | None:
        return self._last_text

    def _refresh(self) -> None:
        recording = self._state is SessionState.RECORDING
        self._action_toggle.setText("Stop dictation" if recording else "Start dictation")
        # Busy states: the toggle would only produce a "still finishing" warning.
        self._action_toggle.setEnabled(self._state in (SessionState.IDLE, SessionState.RECORDING))

        lines = [f"yada — {self._status_line}"]
        if self._shortcut_label:
            lines.append(f"Shortcut: {self._shortcut_label}")
        if self._update_ready:
            lines.append(f"Update {self._update_ready} ready")
        if self._problem:
            # Truncated: a tooltip is not a log, and a provider error can be a paragraph.
            detail = self._problem if len(self._problem) <= 160 else self._problem[:157] + "…"
            lines.append(f"Last problem: {detail}")
        self._tray.setToolTip("\n".join(lines))

    # -- notifications ------------------------------------------------------------------

    def notify(self, title: str, message: str, *, warning: bool = False) -> None:
        """A transient balloon. Used sparingly: errors and warnings only.

        Success is signalled by the chime and the icon, not by a popup -- the point of this
        app is to not break the user's flow.

        Gated here rather than at the call sites so the setting cannot be forgotten by the
        next thing that wants to say something. When notifications are off the message
        still reaches the tooltip and the settings pane; this only suppresses the toast.
        """
        if not self.notifications_enabled:
            return
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QSystemTrayIcon.MessageIcon.Information
        )
        self._tray.showMessage(title, message, icon, 4000)

    # -- events -------------------------------------------------------------------------

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Trigger is a left click on Windows and most Linux trays. This is the behaviour
        # Whispering intends and leaves unimplemented.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_requested.emit()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.settings_requested.emit()


def connect_tray(tray: TrayIcon, *, on_toggle: Callable[[], None]) -> None:
    """Convenience used by app.py; kept here so the signal names live in one place."""
    tray.toggle_requested.connect(on_toggle)


def ensure_tray_available() -> str | None:
    """Returns an explanation if the tray is unusable, else None.

    On GNOME this is a real possibility -- StatusNotifierItem needs an AppIndicator
    extension -- so the app must be able to say why it appears to have started and vanished.
    """
    if not QApplication.instance():
        return "no Qt application"
    if QSystemTrayIcon.isSystemTrayAvailable():
        return None
    import sys

    if sys.platform == "win32":
        # Windows always has a notification area, so this means something unusual.
        return (
            "Windows reported no notification area. yada still works via its shortcut, "
            "but there will be no icon to click."
        )
    return (
        "This desktop has no system tray. On GNOME, install the AppIndicator extension; "
        "on KDE Plasma the tray is built in. yada still works via its shortcut, but there "
        "will be no icon."
    )
