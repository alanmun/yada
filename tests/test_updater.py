"""End-to-end tests for the update mechanism.

Real archives, real Ed25519 signatures, a real HTTP server. The updater executes code it
downloaded, so the tests that matter most are the ones asserting it *refuses* to.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import sys
import tarfile
import threading
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


async def test_public_key_is_read_at_call_time_not_import_time(install_root, server,
                                                               signing_key, monkeypatch):
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
