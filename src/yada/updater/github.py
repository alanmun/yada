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
import contextlib
import hashlib
import os
import shutil
import sys
import tarfile
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .core import SwapFailed, is_newer, staging_dir, swap_in, versions_dir

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
    """Stream `url` to `dest`, via a `.part` file so a partial download is never mistaken
    for a finished one.

    Filesystem errors are translated deliberately. Only httpx errors used to be caught, so
    an OSError escaped as a raw "[Errno 2] No such file or directory: ...zip.part" with
    nothing for the user to act on. The interesting case is the archive disappearing
    mid-download: antivirus scans a freshly written executable-bearing archive and can
    delete it before the rename, which is a real and repeated occurrence rather than a
    theoretical one.
    """
    # Unique per process and per call. Two downloads sharing one .part path is a real
    # collision: whichever finished first renamed it away, and the other then failed with
    # "[Errno 2] ... .zip.part" -- which looked like antivirus but was self-inflicted.
    partial = dest.with_suffix(f"{dest.suffix}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateError(f"could not create the download folder: {exc}") from exc

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
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        raise UpdateError(f"download failed: {exc}") from exc
    except OSError as exc:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        raise UpdateError(_download_os_error_message(exc, partial)) from exc

    try:
        partial.replace(dest)
    except FileNotFoundError as exc:
        raise UpdateError(
            "The downloaded update disappeared before it could be saved. Either another "
            "copy of yada cleared the download folder, or security software removed the "
            "file. It will be retried at the next check."
        ) from exc
    except OSError as exc:
        raise UpdateError(_download_os_error_message(exc, partial)) from exc


def _download_os_error_message(exc: OSError, partial: Path) -> str:
    """Turn a filesystem error during download into something actionable."""
    import errno

    if exc.errno == errno.ENOENT:
        return (
            "The download vanished while it was being written. Either another copy of "
            "yada cleared the download folder, or security software removed the file."
        )
    if exc.errno == errno.ENOSPC:
        return f"Not enough disk space to download the update to {partial.parent}."
    if exc.errno in (errno.EACCES, errno.EPERM):
        return f"No permission to write the update to {partial.parent}."
    return f"could not save the download: {exc}"


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
            verify_signature(
                sums_raw, await _fetch_bytes(sig_asset.url), public_key_b64=public_key_b64
            )
        elif not allow_unsigned:
            raise UpdateError(f"release {release.tag} is not signed and allow_unsigned is off")
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


def _describe(target: Path) -> str:
    """What is actually in a directory, for an error message that can be acted on.

    "contains no yada.exe" said nothing about why: whether nothing extracted, whether the
    wrapper folder was still there, or whether one specific file had been taken away again.
    """
    try:
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    except OSError as exc:
        return f"unreadable ({exc})"
    if not entries:
        return "nothing at all"
    shown = ", ".join(entries[:8])
    return shown if len(entries) <= 8 else f"{shown}, and {len(entries) - 8} more"


def _verify_extracted(target: Path, names: list[str]) -> None:
    """Every file the archive promised has to be on disk.

    The archive's checksum was already verified against a signed SHA256SUMS, so the bytes
    were right. A file that is missing now was written and then removed -- on Windows that
    is antivirus reacting to a freshly extracted, unsigned executable, which is the one
    cause a user can actually do something about.
    """
    missing: list[str] = []
    for name in names:
        if name.endswith("/"):
            continue
        if not (target / name).exists():
            missing.append(name)
            if len(missing) >= 4:
                break
    if not missing:
        return
    listed = ", ".join(missing)
    raise UpdateError(
        f"{len(missing)} file(s) went missing while extracting the release ({listed}). "
        "The download itself was verified, so something removed them afterwards -- "
        "antivirus software is the usual cause. Check its protection history, and see "
        "`yada doctor` for what it has quarantined."
    )


def extract_release(archive: Path, version: str) -> Path:
    """Unpack into versions/<version>/, leaving no trace of whatever was there before.

    Extraction happens in a fresh directory that is then renamed into place, so a version
    directory is only ever absent, or complete and made of exactly one release. Merging new
    files into a partially deleted old directory is the corruption this avoids; an
    interrupted extraction leaves an ignorable `.incoming-` directory instead.

    The `.complete` marker is written last and is the only thing the launcher trusts.
    """
    versions = versions_dir()
    versions.mkdir(parents=True, exist_ok=True)
    target = versions / version
    staging_target = versions / f".incoming-{version}-{os.getpid()}"

    shutil.rmtree(staging_target, ignore_errors=True)
    staging_target.mkdir()  # no exist_ok: this must be a directory we just created

    try:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                _safe_members(names)
                zf.extractall(staging_target)
        else:
            with tarfile.open(archive) as tf:
                names = tf.getnames()
                _safe_members(names)
                tf.extractall(staging_target, filter="data")

        # Checked against the archive's own listing before anything is moved. The archive
        # is already known to be byte-correct at this point -- its checksum was verified
        # against a signed SHA256SUMS -- so a file missing here was removed *after* being
        # written, and naming it is the difference between a cause and a shrug.
        _verify_extracted(staging_target, names)

        _flatten_single_dir(staging_target)

        exe = staging_target / ("yada.exe" if sys.platform == "win32" else "yada")
        if not exe.exists():
            raise UpdateError(
                f"extracted release {version} contains no {exe.name}. "
                f"The extracted folder holds: {_describe(staging_target)}"
            )
        if sys.platform != "win32":
            exe.chmod(0o755)

        try:
            swap_in(staging_target, target)
        except SwapFailed as exc:
            raise UpdateError(
                f"could not install {version} over the existing directory ({exc}). "
                "The version already there was left intact."
            ) from exc
    except UpdateError:
        shutil.rmtree(staging_target, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(staging_target, ignore_errors=True)
        raise UpdateError(f"could not extract {archive.name}: {exc}") from exc

    (target / ".complete").write_text(version + "\n", encoding="utf-8")
    archive.unlink(missing_ok=True)
    return target


def _flatten_single_dir(target: Path) -> None:
    """Collapse an archive that wraps everything in one top-level folder.

    Renaming that folder aside first is not tidiness, it is required. yada's archives put
    everything under `yada/`, and on Linux the executable inside is also called `yada` --
    so moving it to `target/yada` names the very directory being emptied. shutil.move
    then tries to move the file *into* it and fails with "Destination path already
    exists", which broke every Linux update. On Windows the executable is `yada.exe`, so
    the collision never happened there and the bug hid.
    """
    entries = [p for p in target.iterdir() if p.name != ".complete"]
    if len(entries) != 1 or not entries[0].is_dir():
        return
    inner = entries[0]

    holding = target / f".unwrap-{os.getpid()}"
    try:
        inner.rename(holding)
    except OSError as exc:
        raise UpdateError(f"could not unwrap {inner.name}: {exc}") from exc

    for item in sorted(holding.iterdir()):
        destination = target / item.name
        try:
            shutil.move(str(item), str(destination))
        except FileNotFoundError as exc:
            # The listing was taken a moment ago. A file disappearing between then and now
            # means something else deleted it -- on Windows, antivirus removing a file it
            # dislikes from the freshly extracted archive. Continuing would install a
            # release with pieces missing.
            raise UpdateError(
                f"{item.name} disappeared while unpacking the release. Antivirus software "
                "may have removed it; check your security software's protection history."
            ) from exc
        except OSError as exc:
            raise UpdateError(f"could not unpack {item.name}: {exc}") from exc

    with contextlib.suppress(OSError):
        holding.rmdir()


# Anything left in staging older than this is certainly abandoned: a download that has
# made no progress for hours is not going to finish.
STALE_DOWNLOAD_SECONDS = 6 * 3600


def clear_stale_downloads(max_age_seconds: float = STALE_DOWNLOAD_SECONDS) -> list[str]:
    """Delete abandoned downloads, leaving anything recent alone.

    Emphatically not a blanket wipe of the folder. Doing that at startup destroyed a
    download another copy of yada had in flight, and the victim then failed with a bare
    "[Errno 2] ... .zip.part" that read as antivirus interference.
    """
    directory = staging_dir()
    if not directory.is_dir():
        return []
    cutoff = time.time() - max_age_seconds
    removed: list[str] = []
    for item in directory.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed.append(item.name)
        except OSError:
            continue
    return removed
