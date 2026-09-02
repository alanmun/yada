"""Handing over to a newer installed version, without a launcher binary.

yada used to ship a one-file PyInstaller executable as a fixed entry point at the install
root. Windows Defender classified it as Trojan:Win32/Bearfoos.A!ml -- a machine-learning
heuristic, not a signature -- and removed it along with the Start Menu shortcut and the
autostart key about ninety seconds after install. The one-*dir* application beside it was
never touched.

So the redirect lives in the app now, and these are the behaviours that used to be the
launcher's: pick the newest usable version, ignore an incomplete one, and stop choosing a
version that never starts successfully.
"""

from __future__ import annotations

import sys

import pytest

from yada import relaunch
from yada.updater import core


@pytest.fixture
def install(tmp_path, monkeypatch):
    monkeypatch.setenv("YADA_INSTALL_ROOT", str(tmp_path))
    monkeypatch.delenv("YADA_NO_REDIRECT", raising=False)
    return tmp_path


def _install_version(version: str, *, complete: bool = True):
    d = core.versions_dir() / version
    d.mkdir(parents=True, exist_ok=True)
    (d / core.executable_name()).write_text("#!/bin/sh\n")
    if complete:
        (d / ".complete").write_text(version)
    return d


def _pretend_running_from(directory, monkeypatch):
    """Make relaunch believe this process is the frozen app in `directory`."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(directory / core.executable_name()))


def test_no_redirect_when_already_the_newest(install, monkeypatch):
    current = _install_version("0.2.0")
    core.write_current("0.2.0")
    _pretend_running_from(current, monkeypatch)
    assert relaunch.newer_staged_version() is None


def test_redirects_to_a_staged_newer_version(install, monkeypatch):
    old = _install_version("0.1.0")
    _install_version("0.2.0")
    core.write_current("0.1.0")
    _pretend_running_from(old, monkeypatch)

    target = relaunch.newer_staged_version()
    assert target is not None and target.version == "0.2.0"


def test_an_incomplete_version_is_never_chosen(install, monkeypatch):
    old = _install_version("0.1.0")
    _install_version("0.2.0", complete=False)  # interrupted extraction
    core.write_current("0.1.0")
    _pretend_running_from(old, monkeypatch)
    assert relaunch.newer_staged_version() is None


def test_a_version_that_never_starts_is_abandoned(install, monkeypatch):
    old = _install_version("0.1.0")
    _install_version("0.2.0")
    core.write_current("0.1.0")
    core.mark_healthy("0.1.0")
    _pretend_running_from(old, monkeypatch)

    for _ in range(core.MAX_LAUNCH_ATTEMPTS):
        assert relaunch.newer_staged_version().version == "0.2.0"
        core.note_launch_attempt("0.2.0")

    assert relaunch.newer_staged_version() is None, (
        "a release that never reports healthy must stop being handed over to"
    )


def test_a_source_checkout_never_redirects(install, monkeypatch):
    _install_version("0.2.0")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert relaunch.newer_staged_version() is None
    assert relaunch.redirect_if_newer([]) is False


def test_the_escape_hatch_disables_redirection(install, monkeypatch):
    old = _install_version("0.1.0")
    _install_version("0.2.0")
    _pretend_running_from(old, monkeypatch)
    monkeypatch.setenv("YADA_NO_REDIRECT", "1")
    assert relaunch.redirect_if_newer([]) is False


def test_hand_over_records_the_attempt_and_moves_the_pointer(install, monkeypatch):
    old = _install_version("0.1.0")
    _install_version("0.2.0")
    core.write_current("0.1.0")
    _pretend_running_from(old, monkeypatch)

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        relaunch.subprocess, "Popen", lambda cmd, **kw: spawned.append(list(cmd))
    )

    target = relaunch.newer_staged_version()
    assert relaunch.hand_over(target, ["toggle"]) is True
    assert spawned and spawned[0][-1] == "toggle"
    assert core.read_current() == "0.2.0", "the pointer follows the version handed over to"
    assert core._load_state()["versions"]["0.2.0"]["attempts"] == 1


def test_claim_healthy_marks_the_running_version(install, monkeypatch):
    current = _install_version("0.3.0")
    _pretend_running_from(current, monkeypatch)
    relaunch.claim_healthy()
    assert core._load_state()["versions"]["0.3.0"]["healthy"] is True
