"""Release discovery, download, verification and extraction from GitHub Releases.

Security posture, stated plainly because this module executes code it downloaded:

* HTTPS to api.github.com and the asset host, with the repository pinned in config.
* SHA-256 of every archive checked against a `SHA256SUMS` release asset.
* `SHA256SUMS` itself checked against an Ed25519 signature (`SHA256SUMS.sig`) using a
  public key compiled into the binary.

The signature is the part that matters. HTTPS proves the bytes came from GitHub; it says
nothing about whether the release was published by the maintainer or by someone who got
hold of the account. An unsigned archive is refused unless `allow_unsigned` is explicitly
set, which exists for local test builds and nothing else.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import shutil
import sys
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .core import install_root, is_newer, staging_dir, versions_dir

GITHUB_API = "https://api.github.com"
CHECKSUM_ASSET = "SHA256SUMS"
SIGNATURE_ASSET = "SHA256SUMS.sig"

# Ed25519 public key (base64, 32 raw bytes) matching the release-signing private key held
# by CI. Empty until release signing is set up; while empty, updates require
# allow_unsigned=True, so the app cannot silently auto-execute unverified code.
RELEASE_PUBLIC_KEY_B64 = "F5fn7kam56z7MD2oe9BZuTotEJHSHCYJXzlaLOE4m3w="


class UpdateError(Exception):
    """Anything that should abort an update attempt without disturbing the running app."""


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    url: str
    size: int


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    tag: str
    notes: str
    prerelease: bool
    assets: tuple[Asset, ...]

    def asset(self, name: str) -> Asset | None:
        return next((a for a in self.assets if a.name == name), None)

    def platform_asset(self) -> Asset | None:
        """Pick this platform's archive.

        Matched by substring rather than an exact filename so the release naming scheme can
        change without breaking already-installed clients -- which is the whole point of an
        updater.
        """
        wanted = ("windows", ".zip") if sys.platform == "win32" else ("linux", ".tar.gz")
        for a in self.assets:
            low = a.name.lower()
            if all(token in low for token in wanted):
                return a
        return None


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------


def _parse_release(row: dict) -> Release:
    return Release(
        version=str(row.get("tag_name", "")).lstrip("vV"),
        tag=str(row.get("tag_name", "")),
        notes=str(row.get("body") or ""),
        prerelease=bool(row.get("prerelease")),
        assets=tuple(
            Asset(name=a["name"], url=a["browser_download_url"], size=int(a.get("size", 0)))
            for a in row.get("assets") or []
            if a.get("name") and a.get("browser_download_url")
        ),
    )


async def fetch_latest(repo: str, *, include_prerelease: bool = False) -> Release | None:
    """Latest release for `repo` ('owner/name'), or None if there is none.

    Network failures raise UpdateError rather than propagating httpx internals, so callers
    can treat "no update this time" as unremarkable.
    """
    path = f"/repos/{repo}/releases" if include_prerelease else f"/repos/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(base_url=GITHUB_API, headers=headers, timeout=30.0) as c:
            resp = await c.get(path)
    except httpx.HTTPError as exc:
        raise UpdateError(f"could not reach GitHub: {exc}") from exc

    if resp.status_code == 404:
        return None
    if resp.status_code == 403:
        # Unauthenticated API allows 60 requests/hour per IP. Hitting it is not an error
        # worth surfacing; the next scheduled check will succeed.
        raise UpdateError("GitHub API rate limit reached")
    if resp.status_code >= 400:
        raise UpdateError(f"GitHub returned {resp.status_code}")

    body = resp.json()
    if include_prerelease:
        rows = [r for r in body if not r.get("draft")]
        return _parse_release(rows[0]) if rows else None
    return _parse_release(body)


def update_available(release: Release, running_version: str) -> bool:
    return bool(release.version) and is_newer(release.version, running_version)


# --------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def parse_checksums(text: str) -> dict[str, str]:
    """Read `<sha256>  <filename>` lines, as produced by sha256sum."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            out[parts[-1].lstrip("*")] = parts[0].lower()
    return out


def verify_signature(payload: bytes, signature: bytes, *, public_key_b64: str) -> None:
    """Ed25519-verify `payload`, raising UpdateError on any failure."""
    if not public_key_b64:
        raise UpdateError("no release public key compiled in")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise UpdateError("cryptography is required to verify release signatures") from exc

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    except Exception as exc:
        raise UpdateError(f"malformed release public key: {exc}") from exc
    try:
        key.verify(signature, payload)
    except InvalidSignature as exc:
        raise UpdateError("release signature does not match — refusing to install") from exc


# --------------------------------------------------------------------------------------
# Download and stage
# --------------------------------------------------------------------------------------

ProgressFn = Callable[[int, int], None]


async def _download(url: str, dest: Path, *, on_progress: ProgressFn | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    try:
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=None) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                raise UpdateError(f"download failed: HTTP {resp.status_code}")
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with partial.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"download failed: {exc}") from exc
    # Only becomes the real filename once complete, so a partial file is never mistaken
    # for a finished one.
    partial.replace(dest)


async def _fetch_bytes(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise UpdateError(f"could not fetch {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise UpdateError(f"could not fetch {url}: HTTP {resp.status_code}")
    return resp.content


async def download_and_verify(
    release: Release,
    *,
    allow_unsigned: bool = False,
    public_key_b64: str | None = None,
    on_progress: ProgressFn | None = None,
) -> Path:
    """Fetch this platform's archive and prove it is authentic. Returns the archive path.

    `public_key_b64` resolves to RELEASE_PUBLIC_KEY_B64 when omitted. It is looked up here
    rather than used as a default argument value on purpose: a default would bind the
    constant at import time, which silently freezes it and makes the key impossible to
    override or to exercise in a test.
    """
    if public_key_b64 is None:
        public_key_b64 = RELEASE_PUBLIC_KEY_B64
    asset = release.platform_asset()
    if asset is None:
        raise UpdateError(f"release {release.tag} has no asset for this platform")

    sums_asset = release.asset(CHECKSUM_ASSET)
    sig_asset = release.asset(SIGNATURE_ASSET)

    if sums_asset is None:
        if not allow_unsigned:
            raise UpdateError(f"release {release.tag} has no {CHECKSUM_ASSET}")
        checksums: dict[str, str] = {}
    else:
        sums_raw = await _fetch_bytes(sums_asset.url)
        if sig_asset is not None and public_key_b64:
            verify_signature(sums_raw, await _fetch_bytes(sig_asset.url),
                             public_key_b64=public_key_b64)
        elif not allow_unsigned:
            raise UpdateError(
                f"release {release.tag} is not signed and allow_unsigned is off"
            )
        checksums = parse_checksums(sums_raw.decode("utf-8", "replace"))

    archive = staging_dir() / asset.name
    await _download(asset.url, archive, on_progress=on_progress)

    expected = checksums.get(asset.name)
    if expected is None:
        if not allow_unsigned:
            archive.unlink(missing_ok=True)
            raise UpdateError(f"{asset.name} is absent from {CHECKSUM_ASSET}")
    else:
        actual = await asyncio.to_thread(sha256_file, archive)
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise UpdateError(f"checksum mismatch for {asset.name} — refusing to install")
    return archive


# --------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------


def _safe_members(names: list[str]) -> None:
    """Reject path traversal before writing anything to disk."""
    for name in names:
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            raise UpdateError(f"archive contains an unsafe path: {name}")


def extract_release(archive: Path, version: str) -> Path:
    """Unpack into versions/<version>/ and mark it complete.

    The `.complete` marker is written last and is the only thing the launcher trusts, so a
    crash mid-extraction leaves a directory that is simply ignored rather than a
    half-installed release that boots.
    """
    target = versions_dir() / version
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    try:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                _safe_members(zf.namelist())
                zf.extractall(target)
        else:
            with tarfile.open(archive) as tf:
                _safe_members(tf.getnames())
                tf.extractall(target, filter="data")
    except UpdateError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise UpdateError(f"could not extract {archive.name}: {exc}") from exc

    _flatten_single_dir(target)

    exe = target / ("yada.exe" if sys.platform == "win32" else "yada")
    if not exe.exists():
        shutil.rmtree(target, ignore_errors=True)
        raise UpdateError(f"extracted release {version} contains no {exe.name}")
    if sys.platform != "win32":
        exe.chmod(0o755)

    (target / ".complete").write_text(version + "\n", encoding="utf-8")
    archive.unlink(missing_ok=True)
    return target


def _flatten_single_dir(target: Path) -> None:
    """Collapse an archive that wraps everything in one top-level folder."""
    entries = [p for p in target.iterdir() if p.name != ".complete"]
    if len(entries) != 1 or not entries[0].is_dir():
        return
    inner = entries[0]
    for item in list(inner.iterdir()):
        shutil.move(str(item), str(target / item.name))
    inner.rmdir()


def clear_staging() -> None:
    shutil.rmtree(staging_dir(), ignore_errors=True)
    install_root().mkdir(parents=True, exist_ok=True)
