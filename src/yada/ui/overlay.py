"""A small always-on-top panel showing the live transcript while you dictate.

Without this, "transcribe while I speak" had no visible effect whatsoever. The session
emitted partial text on every delta and *nothing was connected to it* -- so streaming could
be working perfectly and the only evidence would be that the final transcript arrived
quickly. Reported, understandably, as "it clearly is not transcribing live".

It also carries the reason when live transcription is not available, because that warning
went to a desktop notification and nowhere else, and notifications are off by default on
Windows.

Two constraints shape everything here:

* **It must never take focus.** yada pastes into whichever window you were using, so a
  panel that activated itself would break the one thing the app exists to do. Hence the
  Tool window type, `WA_ShowWithoutActivating`, and a NoFocus policy.
* **It must not be clickable furniture.** `WA_TransparentForMouseEvents` means clicks pass
  straight through to whatever is underneath, so it can never be in the way.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

# How long the finished transcript stays on screen before the panel disappears.
LINGER_MS = 2500
# A long dictation would otherwise grow the panel without limit.
MAX_CHARS = 320


class LiveOverlay(QWidget):
    """Shows dictation state and live text. Owned by the app; one instance."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.status = QLabel("")
        status_font = QFont(self.font())
        status_font.setBold(True)
        self.status.setFont(status_font)
        self.status.setWordWrap(True)

        self.text = QLabel("")
        self.text.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(6)
        layout.addWidget(self.status)
        layout.addWidget(self.text)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.dismiss)

        self._enabled = True

    # -- configuration ------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self.dismiss()

    # -- lifecycle ----------------------------------------------------------------------

    def begin(self, status: str = "Listening…") -> None:
        self._hide_timer.stop()
        self.status.setText(status)
        self.text.setText("")
        self._present()

    def set_status(self, status: str) -> None:
        self.status.setText(status)
        if self.isVisible():
            self._present()

    def set_partial(self, text: str) -> None:
        self.status.setText("Transcribing live…")
        self.text.setText(_tail(text))
        self._present()

    def finish(self, text: str, *, status: str = "Done") -> None:
        self.status.setText(status)
        self.text.setText(_tail(text))
        self._present()
        self._hide_timer.start(LINGER_MS)

    def report(self, message: str) -> None:
        """Show a problem, whether or not a dictation is in progress."""
        self.status.setText("yada")
        self.text.setText(_tail(message))
        self._present()
        self._hide_timer.start(LINGER_MS * 2)

    def dismiss(self) -> None:
        self._hide_timer.stop()
        self.hide()

    # -- placement and painting ---------------------------------------------------------

    def _present(self) -> None:
        if not self._enabled:
            return
        self.adjustSize()
        self._reposition()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def _reposition(self) -> None:
        """Bottom-centre of whichever screen the pointer is on.

        Following the pointer rather than the primary screen: on a multi-monitor desk the
        window being dictated into is the one being looked at.
        """
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        width = min(max(self.sizeHint().width(), 360), int(available.width() * 0.6))
        self.setFixedWidth(width)
        self.adjustSize()
        x = available.center().x() - self.width() // 2
        y = available.bottom() - self.height() - max(24, int(available.height() * 0.06))
        self.move(x, y)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette = self.palette()
        background = QColor(palette.color(self.backgroundRole()))
        background.setAlpha(235)  # readable over anything, without hiding it completely
        painter.setBrush(background)
        painter.setPen(QPen(palette.color(palette.ColorRole.Highlight), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 10, 10)


def _tail(text: str) -> str:
    """Keep the most recent words: that is where a live transcript is being written."""
    text = " ".join((text or "").split())
    if len(text) <= MAX_CHARS:
        return text
    return "…" + text[-MAX_CHARS:]
