"""`yada doctor` must never be the thing that hangs.

It exists to explain why the app will not work on a given machine, and several of its
checks touch hardware or the window system -- PortAudio device enumeration and
QApplication construction can both block indefinitely depending on drivers and session
type. A diagnostic tool that hangs silently is worse than no tool at all.
"""

from __future__ import annotations

import threading
import time

from yada import doctor


def test_a_stalled_check_is_abandoned_not_fatal(monkeypatch):
    monkeypatch.setattr(doctor, "CHECK_TIMEOUT_SECONDS", 0.3)

    def wedged():
        time.sleep(30)
        return []

    started = time.monotonic()
    result = doctor._run_group("Wedged", wedged)
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"must give up promptly, took {elapsed:.1f}s"
    assert len(result) == 1
    assert result[0].status == doctor.FAIL
    assert "did not finish" in result[0].detail


def test_the_abandoned_thread_cannot_block_process_exit(monkeypatch):
    monkeypatch.setattr(doctor, "CHECK_TIMEOUT_SECONDS", 0.2)
    doctor._run_group("Wedged", lambda: time.sleep(30) or [])
    leftover = [t for t in threading.enumerate() if t.name.startswith("doctor-")]
    assert leftover, "sanity: the stalled thread is still running"
    assert all(t.daemon for t in leftover), "stalled checks must be daemon threads"


def test_a_raising_check_is_reported_not_propagated():
    def broken():
        raise RuntimeError("driver exploded")

    result = doctor._run_group("Broken", broken)
    assert len(result) == 1
    assert result[0].status == doctor.FAIL
    assert "driver exploded" in result[0].detail


def test_checks_are_yielded_incrementally(monkeypatch):
    """Output must appear as checks finish, so a stall is attributable to a named check."""
    seen: list[str] = []

    def slow_group():
        seen.append("ran")
        return [doctor.Check("Slow", doctor.OK, "fine")]

    monkeypatch.setattr(doctor, "_platform_checks", lambda: [doctor.Check("P", doctor.OK, "x")])
    monkeypatch.setattr(doctor, "_audio_checks", slow_group)
    monkeypatch.setattr(doctor, "_qt_checks", lambda: [])
    monkeypatch.setattr(doctor, "_hotkey_checks", lambda: [])
    monkeypatch.setattr(doctor, "_paste_checks", lambda: [])
    monkeypatch.setattr(doctor, "_credential_checks", lambda: [])
    monkeypatch.setattr(doctor, "_install_checks", lambda: [])
    monkeypatch.setattr(doctor, "_path_checks", lambda: [])

    iterator = doctor.iter_checks()
    first = next(iterator)
    assert first.name == "P"
    assert seen == [], "the second group must not have run before the first was yielded"
    assert next(iterator).name == "Slow"
    assert seen == ["ran"]


def test_every_group_is_covered_by_a_deadline():
    """A group added without going through _run_group would reintroduce the hang."""
    import inspect

    source = inspect.getsource(doctor.iter_checks)
    assert "_run_group" in source
    assert source.count("yield") >= 1


def test_console_streams_are_forced_to_utf8(monkeypatch):
    """Windows consoles default to cp1252, which cannot encode our own help text.

    An earlier fix returned early whenever stdout was already usable, so a redirected
    stream -- `yada doctor > out.txt`, or any CI capture -- never got reconfigured and
    kept raising UnicodeEncodeError partway through the report.
    """
    import io
    import sys

    from yada import __main__ as entry

    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    calls: list[tuple[str, object]] = []

    class FakeKernel32:
        @staticmethod
        def SetConsoleOutputCP(cp):
            calls.append(("SetConsoleOutputCP", cp))
            return 1

    class FakeCtypes:
        class windll:
            kernel32 = FakeKernel32()

    entry._force_utf8_streams(FakeCtypes)

    assert ("SetConsoleOutputCP", 65001) in calls
    assert stream.encoding == "utf-8"
    assert stream.errors == "replace"
    # The exact character that killed doctor on Windows.
    stream.write("Open Settings → Providers")
    stream.flush()


def test_prepare_console_is_a_noop_off_windows(monkeypatch):
    import sys

    from yada import __main__ as entry

    monkeypatch.setattr(sys, "platform", "linux")
    entry._prepare_console()  # must not raise or touch anything
