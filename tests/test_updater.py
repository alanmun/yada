"""End-to-end tests for the update mechanism.

Real archives, real Ed25519 signatures, a real HTTP server. The updater executes code it
downloaded, so the tests that matter most are the ones asserting it *refuses* to.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import tarfile
import threading
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


def _make_archive(dirpath: Path, version: str, *, body: str = "#!/bin/sh\necho yada\n") -> Path:
    """A plausible release tarball: one top-level dir containing the executable."""
    stage = dirpath / f"yada-{version}"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "yada").write_text(body)
    (stage / "lib.so").write_text("x" * 128)
    archive = dirpath / f"yada-{version}-linux-x86_64.tar.gz"
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

    assert (target / "yada").exists(), "executable must land at the top level"
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
    evil = tmp_path / "evil-linux-x86_64.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("pwned")
    with tarfile.open(evil, "w:gz") as tf:
        tf.add(payload, arcname="../../../../tmp/pwned")
    with pytest.raises(github.UpdateError, match="unsafe path"):
        github.extract_release(evil, "9.9.9")
    assert not (core.versions_dir() / "9.9.9").exists()


def test_extraction_without_executable_is_rejected(install_root, tmp_path):
    bad = tmp_path / "nobin-linux-x86_64.tar.gz"
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
