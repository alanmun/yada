"""A packaged build must bind PortAudio to the host's ALSA, not the bundled copy."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pytest

from yada.audio import hostaudio


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(hostaudio, "_done", False)
    monkeypatch.setattr(sys, "platform", "linux")


def test_a_source_checkout_is_left_alone(monkeypatch):
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: pytest.fail("must not load anything"))
    assert hostaudio.prefer_host_alsa(frozen=False) == ""


def test_the_first_loadable_host_library_wins(monkeypatch, tmp_path):
    wrong_arch, good = tmp_path / "lib", tmp_path / "lib64"
    for d in (wrong_arch, good):
        d.mkdir()
        (d / hostaudio.SONAME).write_bytes(b"")
    loaded = []

    def cdll(path, mode=0):
        if Path(path).parent == wrong_arch:
            raise OSError("wrong ELF class")
        loaded.append((path, mode))

    monkeypatch.setattr(ctypes, "CDLL", cdll)
    dirs = (str(tmp_path / "missing"), str(wrong_arch), str(good))
    assert hostaudio.prefer_host_alsa(frozen=True, dirs=dirs) == str(good / hostaudio.SONAME)
    assert loaded == [(str(good / hostaudio.SONAME), ctypes.RTLD_GLOBAL)]


def test_a_host_without_alsa_falls_back_to_the_bundled_copy(monkeypatch, tmp_path):
    assert hostaudio.prefer_host_alsa(frozen=True, dirs=(str(tmp_path),)) == ""


def test_it_is_not_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert hostaudio.prefer_host_alsa(frozen=True) == ""
