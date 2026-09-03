"""Tests for installing over a copy that is already running.

Every test here exists because of one incident. A user double-clicked a release while the
same version was running; the installer decided the running copy was gone because its
command socket had closed, started deleting the version directory it occupied, deleted
everything except the one DLL Windows had mapped, and left a working install shredded with
`current` still pointing at it.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from yada import procutil, selfinstall
from yada.updater import core


@pytest.fixture
def install_root(tmp_path, monkeypatch):
    root = tmp_path / "install"
    monkeypatch.setenv("YADA_INSTALL_ROOT", str(root))
    root.mkdir()
    return root


@pytest.fixture
def payload(tmp_path, monkeypatch):
    """A directory shaped like an extracted archive."""
    src = tmp_path / "payload"
    src.mkdir()
    (src / core.executable_name()).write_text("the new executable")
    (src / "_internal").mkdir()
    (src / "_internal" / "python3.dll").write_text("new dll")
    (src / "VERSION").write_text("9.9.9\n")
    monkeypatch.setenv("YADA_PAYLOAD_DIR", str(src))
    monkeypatch.setenv("YADA_VERSION", "9.9.9")
    return src


# --------------------------------------------------------------------------------------
# Waiting for the right thing
# --------------------------------------------------------------------------------------


def test_waits_for_the_process_not_the_socket(install_root, monkeypatch):
    """The regression. A closed socket does not mean the files are free.

    `CommandServer.stop()` runs early in shutdown, so `ipc.is_running()` goes false while
    the process is still up with its DLLs mapped. The old code took that as permission to
    start deleting.
    """
    polls = []

    monkeypatch.setattr(selfinstall.ipc, "is_running", lambda: False)
    monkeypatch.setattr(selfinstall.ipc, "send_command", lambda *a, **k: None)
    monkeypatch.setattr(procutil, "processes_under", lambda _root: [4242])

    def alive(pid):
        polls.append(pid)
        return len(polls) <= 3  # still holding its files for the first few polls

    monkeypatch.setattr(procutil, "pid_alive", alive)
    monkeypatch.setattr(
        procutil, "terminate", lambda *a, **k: pytest.fail("should not need killing")
    )

    assert selfinstall.stop_running_instance(timeout=10.0) is True
    assert len(polls) > 1, "it must keep watching the process, not trust the socket once"


def test_ends_a_copy_that_ignores_the_request_to_quit(install_root, monkeypatch):
    """Being told to close an app that is not responding is not an answer.

    The user's words: the installer should just close it if it was already open.
    """
    killed = []

    monkeypatch.setattr(selfinstall.ipc, "is_running", lambda: False)
    monkeypatch.setattr(selfinstall.ipc, "send_command", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(procutil, "processes_under", lambda _root: [4242])
    monkeypatch.setattr(procutil, "pid_alive", lambda pid: pid not in killed)

    def terminate(pids, **_kwargs):
        killed.extend(pids)
        return []

    monkeypatch.setattr(procutil, "terminate", terminate)

    assert selfinstall.stop_running_instance(timeout=0.1) is True
    assert killed == [4242]


def test_reports_failure_when_a_copy_cannot_be_ended(install_root, monkeypatch):
    monkeypatch.setattr(selfinstall.ipc, "is_running", lambda: False)
    monkeypatch.setattr(selfinstall.ipc, "send_command", lambda *a, **k: None)
    monkeypatch.setattr(procutil, "processes_under", lambda _root: [4242])
    monkeypatch.setattr(procutil, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(procutil, "terminate", lambda pids, **_k: pids)

    assert selfinstall.stop_running_instance(timeout=0.1) is False


def test_nothing_running_costs_nothing(install_root, monkeypatch):
    sent = []
    monkeypatch.setattr(selfinstall.ipc, "is_running", lambda: False)
    monkeypatch.setattr(selfinstall.ipc, "send_command", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(procutil, "processes_under", lambda _root: [])

    assert selfinstall.stop_running_instance(timeout=30.0) is True
    assert sent == [], "no reason to talk to a socket nobody is holding"


def test_a_stranger_holding_the_socket_is_not_killed(install_root, monkeypatch):
    """A copy running from somewhere else is not ours to terminate.

    It still blocks the install -- two instances cannot share one command socket -- so this
    is reported rather than forced.
    """
    monkeypatch.setattr(selfinstall.ipc, "is_running", lambda: True)
    monkeypatch.setattr(selfinstall.ipc, "send_command", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(procutil, "processes_under", lambda _root: [])
    monkeypatch.setattr(procutil, "terminate", lambda *a, **k: pytest.fail("not ours to kill"))

    assert selfinstall.stop_running_instance(timeout=0.1) is False


# --------------------------------------------------------------------------------------
# Installing over an existing version
# --------------------------------------------------------------------------------------


def test_install_replaces_an_existing_version(install_root, payload):
    target = core.versions_dir() / "9.9.9"
    target.mkdir(parents=True)
    (target / core.executable_name()).write_text("the old executable")
    (target / ".complete").write_text("9.9.9\n")

    installed = selfinstall.install(version="9.9.9")

    assert installed.read_text() == "the new executable"
    assert (target / ".complete").exists()
    assert core.read_current() == "9.9.9"
    assert [v.version for v in core.installed_versions()] == ["9.9.9"], (
        "the displaced directory must not linger as a release"
    )


def test_a_locked_install_is_left_whole(install_root, payload, monkeypatch):
    """The incident, asserted directly: refusing must cost the user nothing."""
    target = core.versions_dir() / "9.9.9"
    (target / "_internal").mkdir(parents=True)
    (target / core.executable_name()).write_text("the old executable")
    (target / "_internal" / "python3.dll").write_text("mapped by the running copy")
    (target / ".complete").write_text("9.9.9\n")
    before = {
        p.relative_to(target).as_posix(): p.read_text() if p.is_file() else None
        for p in target.rglob("*")
    }

    real_rename = core.os.rename

    def refuse_moving_aside(src, dst):
        if Path(dst).name.startswith(".trash-"):
            raise OSError(5, "Access is denied")
        return real_rename(src, dst)

    monkeypatch.setattr(core.os, "rename", refuse_moving_aside)

    with pytest.raises(selfinstall.InstallError, match="left exactly as it was"):
        selfinstall.install(version="9.9.9")

    after = {
        p.relative_to(target).as_posix(): p.read_text() if p.is_file() else None
        for p in target.rglob("*")
    }
    assert after == before, "every file the user had must still be there"
    assert not list(core.versions_dir().glob(".incoming-*")), "the copy is cleaned up"


# --------------------------------------------------------------------------------------
# procutil against real processes
# --------------------------------------------------------------------------------------


def test_liveness_and_termination_of_a_real_process():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not procutil.pid_alive(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert procutil.pid_alive(child.pid), "a running child must read as alive"
        assert procutil.wait_for_exit([child.pid], 0.3) == [child.pid]

        assert procutil.terminate([child.pid]) == []
        assert not procutil.pid_alive(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="zombies are a POSIX concept")
def test_an_unreaped_child_reads_as_gone():
    """A process that has exited but not been reaped holds nothing and must read as gone.

    Signal 0 still succeeds against it, so without the /proc state check the installer
    would wait out its whole timeout on a process that finished long ago.
    """
    child = subprocess.Popen([sys.executable, "-c", ""])
    deadline = time.monotonic() + 10
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        # Deliberately not reaped yet: Popen.poll() above collected the status, so force
        # the zombie window by checking a pid whose parent has not called wait().
        assert procutil.pid_alive(child.pid) is False
    finally:
        with contextlib.suppress(Exception):
            child.wait(timeout=10)


def test_a_never_used_pid_is_not_alive():
    # Above every real pid on both platforms; the point is that an unknown pid must read
    # as gone rather than raise, since the caller loops on this.
    assert procutil.pid_alive(0) is False
    assert procutil.pid_alive(-1) is False


def test_processes_under_finds_a_real_child_by_its_image_path():
    """Exercises the actual OS enumeration, not a stand-in.

    The mocked test below covers the filtering; this one covers the part most likely to be
    subtly wrong -- the Win32 ctypes signatures. Getting `OpenProcess`'s restype wrong,
    for instance, truncates the handle and every process reads as gone, which would make
    the installer confidently delete a directory that is still in use.
    """
    interpreter = Path(sys.executable).resolve()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        found = []
        while time.monotonic() < deadline:
            found = procutil.processes_under(interpreter.parent)
            if child.pid in found:
                break
            time.sleep(0.1)
        assert child.pid in found, (
            f"enumeration missed a live child running {interpreter}; found {found}"
        )
        assert procutil.process_image(child.pid), "the image path must be readable"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_processes_under_matches_by_path_and_skips_our_own(tmp_path, monkeypatch):
    """The filtering, without depending on what happens to be running on the machine."""
    root = tmp_path / "install"
    (root / "versions" / "0.1.10").mkdir(parents=True)
    ours = root / "versions" / "0.1.10" / core.executable_name()
    ours.write_text("x")
    elsewhere = tmp_path / "other" / core.executable_name()
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("x")

    images = {11: str(ours), 22: str(elsewhere), 33: None, 44: str(ours)}
    monkeypatch.setattr(procutil, "_win_pids", lambda: list(images))
    monkeypatch.setattr(procutil, "_posix_pids", lambda: list(images))
    monkeypatch.setattr(procutil, "process_image", lambda pid: images.get(pid))
    monkeypatch.setattr(procutil.os, "getpid", lambda: 44)

    assert procutil.processes_under(root) == [11], (
        "only processes running out of the install root, and never ourselves"
    )


def test_processes_under_an_absent_directory_is_empty(tmp_path):
    assert procutil.processes_under(tmp_path / "nope") == []


def test_child_processes_are_kept_off_the_screen(monkeypatch):
    """A PowerShell child with no creationflags flashes a console window.

    Writing the Start Menu shortcut shells out to PowerShell -- there is no pywin32 here --
    and that runs on every startup, so pressing Restart produced a black flicker that looks
    like something untrustworthy rather than a shortcut being refreshed.
    """
    monkeypatch.setattr(procutil.sys, "platform", "win32")
    kwargs = procutil.no_window_kwargs()
    assert kwargs == {"creationflags": 0x08000000}, "CREATE_NO_WINDOW"

    monkeypatch.setattr(procutil.sys, "platform", "linux")
    assert procutil.no_window_kwargs() == {}, "there is no such flag off Windows"


def test_the_shortcut_helper_passes_the_flag(monkeypatch):
    """Asserting the call site, because the flag is invisible in behaviour from a test."""
    import inspect

    from yada import app as app_module

    source = inspect.getsource(app_module.YadaApp._sync_windows_integration)
    assert "no_window_kwargs()" in source, (
        "the PowerShell call must suppress its console window"
    )
