"""End-to-end tests for the update mechanism.

Real archives, real Ed25519 signatures, a real HTTP server. The updater executes code it
downloaded, so the tests that matter most are the ones asserting it *refuses* to.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import http.server
import os
import sys
import tarfile
import threading
import uuid
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yada.updater import core, github


@pytest.fixture
def install_root(tmp_path, monkeypatch):
    root = tmp_path / "install"
    monkeypatch.setenv("YADA_INSTALL_ROOT", str(root))
    root.mkdir()
    return root


@pytest.fixture
def signing_key():
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw()
    return key, base64.b64encode(pub).decode()


# Release assets are named and packed per platform, and `platform_asset()` selects on
# both tokens. Tests that hardcoded the Linux names passed locally and failed the whole
# Windows CI job, so the helpers follow the running platform.
IS_WINDOWS = sys.platform == "win32"
PLATFORM_TAG = "windows-x86_64" if IS_WINDOWS else "linux-x86_64"
ARCHIVE_EXT = ".zip" if IS_WINDOWS else ".tar.gz"


def _make_archive(dirpath: Path, version: str, *, body: str = "#!/bin/sh\necho yada\n") -> Path:
    """A plausible release archive: one top-level dir containing the executable."""
    stage = dirpath / f"yada-{version}"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / core.executable_name()).write_text(body)
    (stage / "lib.so").write_text("x" * 128)
    archive = dirpath / f"yada-{version}-{PLATFORM_TAG}{ARCHIVE_EXT}"
    if IS_WINDOWS:
        with zipfile.ZipFile(archive, "w") as zf:
            for item in sorted(stage.rglob("*")):
                if item.is_file():
                    zf.write(item, item.relative_to(dirpath))
    else:
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(stage, arcname=stage.name)
    return archive


@pytest.fixture
def server(tmp_path):
    """Serve a directory over HTTP so downloads exercise the real code path."""
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(serve_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield serve_dir, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _publish(serve_dir: Path, base_url: str, version: str, key, *, sign=True, tamper=False):
    """Build a release in the served dir and return the matching Release object."""
    archive = _make_archive(serve_dir, version)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if tamper:
        # Bytes change after the manifest is written -- exactly the attack the hash catches.
        archive.write_bytes(archive.read_bytes() + b"malicious")
    sums = f"{digest}  {archive.name}\n".encode()
    (serve_dir / "SHA256SUMS").write_bytes(sums)
    assets = [
        github.Asset(archive.name, f"{base_url}/{archive.name}", archive.stat().st_size),
        github.Asset("SHA256SUMS", f"{base_url}/SHA256SUMS", len(sums)),
    ]
    if sign:
        sig = key.sign(sums)
        (serve_dir / "SHA256SUMS.sig").write_bytes(sig)
        assets.append(github.Asset("SHA256SUMS.sig", f"{base_url}/SHA256SUMS.sig", len(sig)))
    return github.Release(
        version=version, tag=f"v{version}", notes="", prerelease=False, assets=tuple(assets)
    )


# --------------------------------------------------------------------------------------
# Version comparison
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("1.2.3", "1.2.2", True),
        ("v1.10.0", "1.9.9", True),
        ("1.2.3", "1.2.3", False),
        ("1.2.3-beta.1", "1.2.3", False),  # prerelease suffix ignored -> equal, not newer
        ("2.0.0", "1.99.99", True),
        ("garbage", "1.0.0", False),  # unparseable sorts lowest instead of raising
    ],
)
def test_version_comparison(a, b, expected):
    assert core.is_newer(a, b) is expected


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


async def test_signed_release_downloads_verifies_and_extracts(install_root, server, signing_key):
    serve_dir, base_url = server
    key, pub = signing_key
    release = _publish(serve_dir, base_url, "0.3.2", key)

    archive = await github.download_and_verify(release, public_key_b64=pub)
    target = github.extract_release(archive, "0.3.2")

    exe = core.executable_name()  # "yada.exe" on Windows
    assert (target / exe).exists(), "executable must land at the top level"
    assert not (target / "yada-0.3.2").exists(), "single wrapper dir should be flattened"
    assert (target / ".complete").exists(), "completion marker is what the launcher trusts"
    assert not archive.exists(), "archive should be cleaned up after extraction"

    installed = core.installed_versions()
    assert [v.version for v in installed] == ["0.3.2"]
    assert installed[0].complete


# --------------------------------------------------------------------------------------
# Refusals -- the tests that actually matter
# --------------------------------------------------------------------------------------


async def test_tampered_archive_is_refused(install_root, server, signing_key):
    serve_dir, base_url = server
    key, pub = signing_key
    release = _publish(serve_dir, base_url, "0.3.3", key, tamper=True)

    with pytest.raises(github.UpdateError, match="checksum mismatch"):
        await github.download_and_verify(release, public_key_b64=pub)
    assert not list(core.staging_dir().glob("*.tar.gz")), "bad download must not be kept"


async def test_wrong_signature_is_refused(install_root, server, signing_key):
    serve_dir, base_url = server
    key, _ = signing_key
    release = _publish(serve_dir, base_url, "0.3.4", key)
    # A different key -- i.e. someone who published a release but does not hold ours.
    other_pub = base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    ).decode()

    with pytest.raises(github.UpdateError, match="signature does not match"):
        await github.download_and_verify(release, public_key_b64=other_pub)


async def test_unsigned_release_refused_by_default(install_root, server, signing_key):
    serve_dir, base_url = server
    key, pub = signing_key
    release = _publish(serve_dir, base_url, "0.3.5", key, sign=False)

    with pytest.raises(github.UpdateError, match="not signed"):
        await github.download_and_verify(release, public_key_b64=pub)

    # ...but permitted when explicitly allowed, for local test builds.
    archive = await github.download_and_verify(release, allow_unsigned=True, public_key_b64="")
    assert archive.exists()


async def test_missing_checksums_refused(install_root, server, signing_key):
    serve_dir, base_url = server
    _, pub = signing_key
    archive = _make_archive(serve_dir, "0.3.6")
    release = github.Release(
        version="0.3.6",
        tag="v0.3.6",
        notes="",
        prerelease=False,
        assets=(github.Asset(archive.name, f"{base_url}/{archive.name}", 1),),
    )
    with pytest.raises(github.UpdateError, match="no SHA256SUMS"):
        await github.download_and_verify(release, public_key_b64=pub)


def test_path_traversal_in_archive_is_rejected(install_root, tmp_path):
    evil = tmp_path / f"evil-{PLATFORM_TAG}.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("pwned")
    with tarfile.open(evil, "w:gz") as tf:
        tf.add(payload, arcname="../../../../tmp/pwned")
    with pytest.raises(github.UpdateError, match="unsafe path"):
        github.extract_release(evil, "9.9.9")
    assert not (core.versions_dir() / "9.9.9").exists()


def test_extraction_without_executable_is_rejected(install_root, tmp_path):
    bad = tmp_path / f"nobin-{PLATFORM_TAG}.tar.gz"
    junk = tmp_path / "readme.txt"
    junk.write_text("nothing useful")
    with tarfile.open(bad, "w:gz") as tf:
        tf.add(junk, arcname="readme.txt")
    with pytest.raises(github.UpdateError, match="no yada"):
        github.extract_release(bad, "8.8.8")
    assert not (core.versions_dir() / "8.8.8").exists()


# --------------------------------------------------------------------------------------
# Activation, health and rollback
# --------------------------------------------------------------------------------------


def _install_fake(version: str, *, complete: bool = True) -> Path:
    d = core.versions_dir() / version
    d.mkdir(parents=True, exist_ok=True)
    (d / core.executable_name()).write_text("#!/bin/sh\n")
    if complete:
        (d / ".complete").write_text(version)
    return d


def test_newest_complete_version_is_selected(install_root):
    _install_fake("0.1.0")
    _install_fake("0.2.0")
    core.write_current("0.1.0")
    chosen = core.select_version_to_launch()
    assert chosen is not None and chosen.version == "0.2.0", (
        "a staged newer release must activate itself without an installer step"
    )


def test_incomplete_version_is_never_launched(install_root):
    _install_fake("0.1.0")
    _install_fake("0.2.0", complete=False)  # interrupted extraction
    core.write_current("0.1.0")
    chosen = core.select_version_to_launch()
    assert chosen is not None and chosen.version == "0.1.0"


def test_repeatedly_failing_version_rolls_back(install_root):
    _install_fake("0.1.0")
    _install_fake("0.2.0")
    core.mark_healthy("0.1.0")
    core.write_current("0.1.0")

    for _ in range(core.MAX_LAUNCH_ATTEMPTS):
        chosen = core.select_version_to_launch()
        assert chosen is not None and chosen.version == "0.2.0"
        core.note_launch_attempt("0.2.0")  # started, never reached mark_healthy

    chosen = core.select_version_to_launch()
    assert chosen is not None and chosen.version == "0.1.0", (
        "a release that never starts successfully must stop being chosen"
    )


def test_healthy_version_resets_attempts(install_root):
    _install_fake("0.2.0")
    core.note_launch_attempt("0.2.0")
    core.note_launch_attempt("0.2.0")
    core.mark_healthy("0.2.0")
    assert not core.is_presumed_broken("0.2.0")
    for _ in range(core.MAX_LAUNCH_ATTEMPTS + 2):
        core.note_launch_attempt("0.2.0")
    assert not core.is_presumed_broken("0.2.0"), "a version that has worked stays trusted"


def test_pruning_keeps_running_version(install_root):
    for v in ("0.1.0", "0.2.0", "0.3.0", "0.4.0"):
        _install_fake(v)
    core.write_current("0.1.0")  # oldest is the running one
    core.prune_old_versions(keep=2)
    remaining = {v.version for v in core.installed_versions()}
    assert "0.1.0" in remaining, "must never delete the version currently in use"
    assert "0.4.0" in remaining and "0.3.0" in remaining


async def test_public_key_is_read_at_call_time_not_import_time(
    install_root, server, signing_key, monkeypatch
):
    """The module constant must be live.

    An earlier version took the key as a default argument value, which bound it at import
    time. Setting RELEASE_PUBLIC_KEY_B64 afterwards then had no effect, and a correctly
    signed release was rejected as unsigned.
    """
    serve_dir, base_url = server
    key, pub = signing_key
    release = _publish(serve_dir, base_url, "0.4.0", key)

    monkeypatch.setattr(github, "RELEASE_PUBLIC_KEY_B64", pub)
    archive = await github.download_and_verify(release)  # no explicit key
    assert archive.exists()


# --------------------------------------------------------------------------------------
# Asset selection — the logic that broke Windows CI
# --------------------------------------------------------------------------------------


def _release_with_both_platforms() -> github.Release:
    return github.Release(
        version="1.0.0",
        tag="v1.0.0",
        notes="",
        prerelease=False,
        assets=(
            github.Asset("yada-1.0.0-linux-x86_64.tar.gz", "http://x/l", 1),
            github.Asset("yada-1.0.0-windows-x86_64.zip", "http://x/w", 1),
            github.Asset("SHA256SUMS", "http://x/s", 1),
        ),
    )


def test_platform_asset_picks_the_windows_zip(monkeypatch):
    """Runs on Linux too, so the Windows branch is covered wherever tests run."""
    monkeypatch.setattr(sys, "platform", "win32")
    asset = _release_with_both_platforms().platform_asset()
    assert asset is not None and asset.name.endswith(".zip")
    assert "windows" in asset.name


def test_platform_asset_picks_the_linux_tarball(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    asset = _release_with_both_platforms().platform_asset()
    assert asset is not None and asset.name.endswith(".tar.gz")
    assert "linux" in asset.name


def test_platform_asset_is_none_when_the_release_lacks_this_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    release = github.Release(
        version="1.0.0",
        tag="v1.0.0",
        notes="",
        prerelease=False,
        assets=(github.Asset("yada-1.0.0-linux-x86_64.tar.gz", "http://x/l", 1),),
    )
    assert release.platform_asset() is None


def _pack(dirpath: Path, name: str, *, as_zip: bool) -> Path:
    """Build an archive of either kind, so both extraction branches run on every OS."""
    stage = dirpath / "yada-9.9.9"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / core.executable_name()).write_text("#!/bin/sh\n")
    (stage / "_internal").mkdir(exist_ok=True)
    (stage / "_internal" / "payload.bin").write_bytes(b"\x00" * 64)
    archive = dirpath / name
    if as_zip:
        with zipfile.ZipFile(archive, "w") as zf:
            for item in sorted(stage.rglob("*")):
                if item.is_file():
                    zf.write(item, item.relative_to(dirpath))
    else:
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(stage, arcname=stage.name)
    return archive


@pytest.mark.parametrize("as_zip", [True, False], ids=["zip", "tar.gz"])
def test_both_archive_formats_extract(install_root, tmp_path, as_zip):
    """Linux releases ship .tar.gz and Windows .zip, so each OS would otherwise only ever
    exercise its own branch of extract_release."""
    archive = _pack(tmp_path, f"yada-9.9.9{'.zip' if as_zip else '.tar.gz'}", as_zip=as_zip)
    target = github.extract_release(archive, "9.9.9")

    assert (target / core.executable_name()).exists(), "executable must land at the top level"
    assert (target / "_internal" / "payload.bin").exists(), "bundle contents must survive"
    assert not (target / "yada-9.9.9").exists(), "the single wrapper dir must be flattened"
    assert (target / ".complete").exists()
    assert not archive.exists(), "archive is cleaned up after extraction"


# --------------------------------------------------------------------------------------
# Launch accounting — client commands must not look like failed launches
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Disk retention — a version directory is ~190 MB, so this is not housekeeping trivia
# --------------------------------------------------------------------------------------


def test_pruning_keeps_only_the_newest_few(install_root):
    for v in ("0.1.0", "0.1.1", "0.1.2", "0.1.4", "0.1.5"):
        _install_fake(v)
    core.write_current("0.1.5")

    removed = core.prune_old_versions()
    assert sorted(removed) == ["0.1.0", "0.1.1", "0.1.2"]
    assert [v.version for v in core.installed_versions()] == ["0.1.5", "0.1.4"]


def test_a_stale_current_pointer_pins_its_version(install_root):
    """The active version is never deleted, even when it is old.

    Deliberate: removing what `current` names would leave nothing to fall back to. The
    consequence is that a pointer left behind by running a version directly keeps that
    version on disk, which is worth knowing rather than surprising.
    """
    for v in ("0.1.0", "0.1.1", "0.1.2", "0.1.4", "0.1.5"):
        _install_fake(v)
    core.write_current("0.1.1")

    core.prune_old_versions()
    remaining = [v.version for v in core.installed_versions()]
    assert "0.1.1" in remaining, "the active version must survive pruning"
    assert "0.1.5" in remaining and "0.1.4" in remaining
    assert "0.1.0" not in remaining and "0.1.2" not in remaining


def test_pruning_is_safe_with_nothing_to_prune(install_root):
    _install_fake("0.1.5")
    core.write_current("0.1.5")
    assert core.prune_old_versions() == []
    assert [v.version for v in core.installed_versions()] == ["0.1.5"]


# --------------------------------------------------------------------------------------
# Version isolation — two releases must never blend into one directory
# --------------------------------------------------------------------------------------


def test_reinstalling_a_version_leaves_nothing_from_the_old_one(install_root, tmp_path):
    """The file that matters is one the new archive does not contain.

    Extracting over a previous install used to merge, so a file only the old release had
    survived into the new version's directory.
    """
    target = core.versions_dir() / "9.9.9"
    target.mkdir(parents=True)
    (target / core.executable_name()).write_text("old")
    (target / "leftover-from-the-old-release.dll").write_text("stale")
    (target / ".complete").write_text("9.9.9")

    archive = _pack(tmp_path, "yada-9.9.9.tar.gz", as_zip=False)
    github.extract_release(archive, "9.9.9")

    assert not (target / "leftover-from-the-old-release.dll").exists(), (
        "a file from the previous release must not survive into the new version"
    )
    assert (target / core.executable_name()).read_text() != "old"
    assert (target / "_internal" / "payload.bin").exists()


def test_extraction_refuses_rather_than_merging_when_the_old_dir_cannot_be_replaced(
    install_root, tmp_path, monkeypatch
):
    """Refusing to install beats installing a version made of two -- or made of none.

    Two behaviours have been wrong here. First `ignore_errors=True`, so a locked file
    survived silently and the new files were written alongside it, then marked complete.
    Then a verified `rmtree`, which deletes file by file: a copy running out of the target
    holds one DLL mapped, so everything else was already deleted by the time Windows
    refused, and the user was left with a shredded install that `current` still pointed at.

    The directory is now renamed aside instead, which either works completely or not at
    all. So the assertion is not merely that it refused -- it is that every file the user
    had is still there.
    """
    target = core.versions_dir() / "9.9.9"
    target.mkdir(parents=True)
    (target / "yada.exe").write_text("the working executable")
    (target / "_internal").mkdir()
    (target / "_internal" / "python3.dll").write_text("held open by a running instance")
    (target / ".complete").write_text("9.9.9\n")
    before = sorted(p.relative_to(target).as_posix() for p in target.rglob("*"))

    real_rename = core.os.rename

    def refuse_moving_aside(src, dst):
        # Only the swap's own move. `core.os` is the global os module, so refusing every
        # rename also breaks the extraction that has to happen first -- which is how this
        # test previously failed for the wrong reason.
        if Path(dst).name.startswith(".trash-"):
            raise OSError(5, "Access is denied")
        return real_rename(src, dst)

    monkeypatch.setattr(core.os, "rename", refuse_moving_aside)

    archive = _pack(tmp_path, "yada-9.9.9.tar.gz", as_zip=False)
    with pytest.raises(github.UpdateError, match="left intact"):
        github.extract_release(archive, "9.9.9")

    after = sorted(p.relative_to(target).as_posix() for p in target.rglob("*"))
    assert after == before, "not one file of the working install may be lost"
    assert (target / "yada.exe").read_text() == "the working executable"


def test_a_failed_swap_puts_the_old_version_back(install_root, monkeypatch):
    """If the new files cannot be moved in, the displaced version is restored.

    Otherwise the window between "old moved aside" and "new moved in" is one where a
    crash or an error leaves no install at all.
    """
    target = core.versions_dir() / "9.9.9"
    target.mkdir(parents=True)
    (target / "yada.exe").write_text("the working executable")
    incoming = core.versions_dir() / ".incoming-9.9.9-1234"
    incoming.mkdir()
    (incoming / "yada.exe").write_text("the new executable")

    real_rename = core.os.rename
    calls = []

    def rename_once(src, dst):
        calls.append((src, dst))
        real_rename(src, dst)

    def refuse_replace(src, dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(core.os, "rename", rename_once)
    monkeypatch.setattr(core.os, "replace", refuse_replace)

    with pytest.raises(core.SwapFailed, match="could not move the new files"):
        core.swap_in(incoming, target)

    assert (target / "yada.exe").read_text() == "the working executable", (
        "the displaced version must be restored, not left as a gap"
    )
    assert len(calls) == 2, "moved aside, then moved back"


def test_a_replaced_version_that_cannot_be_deleted_is_left_for_later(install_root):
    """A locked leftover must not fail the install, or count as a release."""
    target = core.versions_dir() / "9.9.9"
    target.mkdir(parents=True)
    (target / "yada.exe").write_text("old")
    (target / ".complete").write_text("9.9.9\n")
    incoming = core.versions_dir() / ".incoming-9.9.9-1234"
    incoming.mkdir()
    (incoming / "yada.exe").write_text("new")
    (incoming / ".complete").write_text("9.9.9\n")

    core.swap_in(incoming, target)

    assert (target / "yada.exe").read_text() == "new"
    assert [v.version for v in core.installed_versions()] == ["9.9.9"], (
        "a .trash- leftover must never appear as an installed version"
    )


def test_prune_clears_trash_left_by_an_earlier_swap(install_root):
    """The leftover a locked file forces us to abandon gets collected on a later run."""
    _install_fake("0.1.5")
    trash = core.versions_dir() / ".trash-0.1.4-9999-abcd1234"
    trash.mkdir(parents=True)
    (trash / "python3.dll").write_text("was mapped at the time")

    assert trash in core.abandoned_extractions()
    core.prune_old_versions()
    assert not trash.exists(), "the leftover is deleted once nothing holds it"
    assert [v.version for v in core.installed_versions()] == ["0.1.5"]


def test_an_interrupted_extraction_is_not_mistaken_for_a_release(install_root):
    _install_fake("0.1.5")
    leftover = core.versions_dir() / ".incoming-0.1.6-4242"
    leftover.mkdir(parents=True)
    (leftover / "half-written.bin").write_text("x")

    assert [v.version for v in core.installed_versions()] == ["0.1.5"], (
        "a half-finished extraction must not appear as an installed version"
    )
    assert core.abandoned_extractions() == [leftover]

    core.write_current("0.1.5")
    core.prune_old_versions()
    assert not leftover.exists(), "pruning clears abandoned extractions"


def test_unwraps_an_archive_whose_top_folder_matches_the_executable(install_root, tmp_path):
    """yada's own archives are exactly this shape, and it broke every Linux update.

    Everything sits under `yada/`, and on Linux the executable inside is also `yada`, so
    flattening moved the file onto the path of the directory being emptied. Windows was
    unaffected because its executable is `yada.exe`, which is why this survived several
    releases.
    """
    stage = tmp_path / "yada"  # top-level folder named the same as the executable
    stage.mkdir()
    (stage / core.executable_name()).write_text("#!/bin/sh\n")
    (stage / "_internal").mkdir()
    (stage / "_internal" / "payload.bin").write_bytes(b"\x00" * 32)
    (stage / "VERSION").write_text("7.7.7\n")

    archive = tmp_path / "yada-7.7.7.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage, arcname="yada")

    target = github.extract_release(archive, "7.7.7")

    assert (target / core.executable_name()).is_file(), "the executable must be a file"
    assert (target / "_internal" / "payload.bin").exists()
    assert (target / "VERSION").exists()
    assert not (target / "yada" / "yada").exists(), "the wrapper folder must be gone"
    assert (target / ".complete").exists()


def test_a_file_vanishing_mid_unpack_is_reported_as_such(install_root, tmp_path, monkeypatch):
    """Antivirus deleting a file out of the extracted archive must say so.

    This surfaced as a bare '[Errno 2] No such file or directory' with nothing to act on.
    """
    stage = tmp_path / "yada"
    stage.mkdir()
    (stage / core.executable_name()).write_text("#!/bin/sh\n")
    (stage / "SOMETHING.bin").write_bytes(b"x")
    archive = tmp_path / "yada-7.7.8.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage, arcname="yada")

    real_move = github.shutil.move

    def move_but_lose_one(src, dst, *a, **k):
        if src.endswith("SOMETHING.bin"):
            raise FileNotFoundError(2, "No such file or directory")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(github.shutil, "move", move_but_lose_one)
    with pytest.raises(github.UpdateError, match="Antivirus"):
        github.extract_release(archive, "7.7.8")


async def test_a_download_removed_mid_flight_is_explained(install_root, server, signing_key):
    """A raw "[Errno 2] ... .zip.part" told the user nothing.

    The real cause on the machine this was reported from was two checks writing the same
    part file, not antivirus -- checked against Defender's detection history, which had
    nothing for that version. So the message names both possibilities and neither as fact.
    """
    serve_dir, base_url = server
    key, pub = signing_key
    release = _publish(serve_dir, base_url, "0.4.1", key)

    real_replace = github.Path.replace

    def vanish(self, target):
        if str(self).endswith(".part"):
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_replace(self, target)

    github.Path.replace = vanish
    try:
        with pytest.raises(github.UpdateError, match="disappeared before it could be saved"):
            await github.download_and_verify(release, public_key_b64=pub)
    finally:
        github.Path.replace = real_replace


async def test_no_disk_space_is_reported_as_such(install_root, server, signing_key):
    import errno

    serve_dir, base_url = server
    key, pub = signing_key
    release = _publish(serve_dir, base_url, "0.4.2", key)

    real_open = github.Path.open

    def full(self, *a, **k):
        if str(self).endswith(".part"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, *a, **k)

    github.Path.open = full
    try:
        with pytest.raises(github.UpdateError, match="disk space"):
            await github.download_and_verify(release, public_key_b64=pub)
    finally:
        github.Path.open = real_open


def test_part_files_are_unique_per_download(install_root):
    """Two downloads must not write the same file.

    They did, and whichever finished first renamed it away, leaving the other to fail with
    "[Errno 2] ... .zip.part" -- which read as antivirus but was self-inflicted.
    """
    dest = core.staging_dir() / "yada-1.0.0-windows-x86_64.zip"
    names = {
        dest.with_suffix(f"{dest.suffix}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part").name
        for _ in range(20)
    }
    assert len(names) == 20, "part file names must not collide"
    assert all(n.endswith(".part") for n in names)


def test_only_stale_downloads_are_cleared(install_root):
    """A recent download may belong to another copy of yada that is still using it."""
    staging = core.staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    fresh = staging / "in-flight.zip.part"
    old = staging / "abandoned.zip.part"
    fresh.write_bytes(b"x")
    old.write_bytes(b"x")
    os.utime(old, (0, 0))  # long abandoned

    removed = github.clear_stale_downloads()

    assert removed == ["abandoned.zip.part"]
    assert fresh.exists(), "a recent download must be left alone"
    assert not old.exists()


async def test_a_second_check_while_one_is_running_is_ignored(install_root, monkeypatch):
    """Asking twice means "tell me now", not "download it twice"."""
    from yada.updater import service as svc

    started = 0

    async def slow_fetch(repo, **kwargs):
        nonlocal started
        started += 1
        await asyncio.sleep(0.2)
        return None

    monkeypatch.setattr(svc, "fetch_latest", slow_fetch)
    s = svc.UpdateService(repo="x/y", current_version="1.0.0")

    await asyncio.gather(s.check_now(), s.check_now(), s.check_now())
    assert started == 1, f"expected one check, {started} ran"


def test_a_file_removed_after_extraction_is_named(install_root, tmp_path, monkeypatch):
    """ "contains no yada.exe" said nothing about why, and we saw it twice.

    The archive's checksum is verified against a signed SHA256SUMS before extraction, so a
    file missing afterwards was written and then taken away -- on Windows, antivirus
    reacting to a freshly extracted unsigned executable. Naming the file is the difference
    between a cause and a shrug.
    """
    archive = _pack(tmp_path, "yada-9.9.9.tar.gz", as_zip=False)

    real_extractall = github.tarfile.TarFile.extractall

    def extract_then_remove(self, path, *args, **kwargs):
        real_extractall(self, path, *args, **kwargs)
        # Whatever the payload's executable is called on this platform.
        for victim in Path(path).rglob(core.executable_name()):
            victim.unlink()

    monkeypatch.setattr(github.tarfile.TarFile, "extractall", extract_then_remove)

    with pytest.raises(github.UpdateError) as caught:
        github.extract_release(archive, "9.9.9")

    message = str(caught.value)
    assert core.executable_name() in message, "the missing file must be named"
    assert "antivirus" in message.lower(), "and the usual cause suggested"
    assert "went missing" in message


def test_a_missing_executable_reports_what_was_there_instead(install_root, tmp_path, monkeypatch):
    """When the tree is intact but the executable is not, say what *is* present."""
    archive = _pack(tmp_path, "yada-9.9.9.tar.gz", as_zip=False)

    # Pass the completeness check, then lose the executable during the flatten.
    monkeypatch.setattr(github, "_verify_extracted", lambda *_a, **_k: None)
    real_flatten = github._flatten_single_dir

    def flatten_then_remove(target):
        real_flatten(target)
        exe = target / core.executable_name()
        if exe.exists():
            exe.unlink()

    monkeypatch.setattr(github, "_flatten_single_dir", flatten_then_remove)

    with pytest.raises(github.UpdateError, match="contains no") as caught:
        github.extract_release(archive, "9.9.9")

    message = str(caught.value)
    assert "extracted folder holds" in message
    assert "_internal" in message, "a listing is what makes this diagnosable"


def test_describe_handles_an_empty_directory(tmp_path):
    assert github._describe(tmp_path) == "nothing at all"
