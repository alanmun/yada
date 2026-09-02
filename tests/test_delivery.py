"""Delivery has to happen on the Qt thread, and the proof is the mechanism.

Qt's clipboard on Windows is OLE-based and requires the GUI thread. It was being called
from the asyncio thread, inside the lock the session holds while finishing a dictation: the
text reached the clipboard and the read-back never returned, so the state sat on
"Transcribing…" indefinitely and the paste never happened.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from yada.app import EventBridge


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_delivery_emitted_from_a_worker_lands_on_the_main_thread(qapp):
    bridge = EventBridge()
    seen: list[str] = []
    payload: list[tuple] = []

    def slot(text, stage):
        seen.append(threading.current_thread().name)
        payload.append((text, stage))

    bridge.deliver_requested.connect(slot, Qt.ConnectionType.QueuedConnection)

    worker = threading.Thread(
        target=lambda: bridge.deliver_requested.emit("the transcript", None),
        name="not-the-gui-thread",
    )
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "emitting must not block the calling thread"

    assert seen == [], "a queued connection must not run the slot inline"
    qapp.processEvents()

    assert seen == [threading.main_thread().name]
    assert payload == [("the transcript", None)]


def test_the_bridge_exposes_delivery_as_a_signal():
    """Called directly it would run on whichever thread finished the dictation."""
    assert hasattr(EventBridge, "deliver_requested")
