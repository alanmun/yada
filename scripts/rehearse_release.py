"""Full release rehearsal, entirely local. Nothing is published.

Run this before tagging a real release. It is the only way to exercise the update path
without shipping two releases to find out it is broken.

    uv run pyinstaller --noconfirm packaging/yada-launcher.spec --distpath dist-test
    uv run python scripts/rehearse_release.py

Exercises the real chain end to end:

    fake GitHub API  ->  fetch_latest  ->  signature + checksum verification
                     ->  extract_release  ->  the real compiled launcher activates it

The only fakes are the GitHub API endpoint and the contents of the app binary. Every piece of
yada's own code is the real one, including the compiled launcher.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

work = Path(tempfile.mkdtemp(prefix="yada-rehearsal-"))
serve = work / "serve"
serve.mkdir(parents=True)
install = work / "install"
os.environ["YADA_INSTALL_ROOT"] = str(install)

from yada.updater import core, github, service  # noqa: E402

print("=" * 74)
print("RELEASE REHEARSAL — nothing leaves this machine")
print("=" * 74)

# ---------------------------------------------------------------- 1. signing key
key = Ed25519PrivateKey.generate()
pub = base64.b64encode(key.public_key().public_bytes_raw()).decode()
github.RELEASE_PUBLIC_KEY_B64 = pub
print(f"\n1. Generated a throwaway signing key (public: {pub[:16]}…)")

# ---------------------------------------------------------------- 2. build a release archive
NEW = "0.1.1"
stage = work / f"yada-{NEW}"
(stage / "_internal").mkdir(parents=True)
(stage / "_internal" / "libfake.so").write_bytes(b"\x00" * 4096)
(stage / "yada").write_text('#!/bin/sh\necho "  >>> yada 0.1.1 running, args: $*"\n')
(stage / "yada").chmod(0o755)
shutil.copy(ROOT / "packaging" / "install.sh", stage / "install.sh")
archive_name = f"yada-{NEW}-linux-x86_64.tar.gz"
with tarfile.open(serve / archive_name, "w:gz") as tf:
    tf.add(stage, arcname=stage.name)
size = (serve / archive_name).stat().st_size
print(f"2. Built {archive_name} ({size:,} bytes), same shape CI produces")

# ---------------------------------------------------------------- 3. checksum + sign
digest = hashlib.sha256((serve / archive_name).read_bytes()).hexdigest()
sums = f"{digest}  {archive_name}\n".encode()
(serve / "SHA256SUMS").write_bytes(sums)
(serve / "SHA256SUMS.sig").write_bytes(key.sign(sums))
print(f"3. Wrote and signed SHA256SUMS (sha256 {digest[:16]}…)")

# ---------------------------------------------------------------- 4. fake the GitHub API
httpd = http.server.ThreadingHTTPServer(
    ("127.0.0.1", 0),
    functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(serve)),
)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"

api_dir = serve / "repos" / "you" / "yada" / "releases"
api_dir.mkdir(parents=True)
(api_dir / "latest").write_text(json.dumps({
    "tag_name": f"v{NEW}", "body": "Rehearsal release.", "prerelease": False,
    "assets": [
        {"name": archive_name, "browser_download_url": f"{base}/{archive_name}", "size": size},
        {"name": "SHA256SUMS", "browser_download_url": f"{base}/SHA256SUMS", "size": len(sums)},
        {"name": "SHA256SUMS.sig", "browser_download_url": f"{base}/SHA256SUMS.sig", "size": 64},
    ],
}))
github.GITHUB_API = base
print(f"4. Serving a fake GitHub releases API at {base}")

# ---------------------------------------------------------------- 5. pretend 0.1.0 is installed
OLD = "0.1.0"
old_dir = install / "versions" / OLD
old_dir.mkdir(parents=True)
(old_dir / "yada").write_text('#!/bin/sh\necho "  >>> yada 0.1.0 running, args: $*"\n')
(old_dir / "yada").chmod(0o755)
(old_dir / ".complete").write_text(OLD)
core.write_current(OLD)
core.mark_healthy(OLD)
launcher = next(
    (p for p in (ROOT / "dist-test" / "yada-launcher", ROOT / "dist" / "yada-launcher")
     if p.exists()),
    None,
)
if launcher is None:
    raise SystemExit(
        "Build the launcher first:\n"
        "  uv run pyinstaller --noconfirm packaging/yada-launcher.spec --distpath dist-test"
    )
shutil.copy(launcher, install / "yada")
(install / "yada").chmod(0o755)
print(f"5. Installed {OLD} with the real compiled launcher")

# ---------------------------------------------------------------- 6. run the real updater
import asyncio  # noqa: E402

progress = []
svc = service.UpdateService(repo="you/yada", current_version=OLD,
                            on_change=lambda st: progress.append(st.summary()))
print("\n6. Running the real UpdateService.check_now()…")
status = asyncio.run(svc.check_now())
print(f"   available: {status.available_version}")
print(f"   ready    : {status.ready_version}")
print(f"   error    : {status.last_error}")
print(f"   summary  : {status.summary()}")
assert status.ready_version == NEW, f"expected {NEW} staged, got {status.ready_version}"

new_dir = install / "versions" / NEW
assert (new_dir / "yada").exists(), "app binary missing from the extracted version"
assert (new_dir / "_internal" / "libfake.so").exists(), "bundle contents missing"
assert (new_dir / ".complete").exists(), "completion marker missing"
assert not list((install / "staging").glob("*.tar.gz")), "archive should be cleaned up"
print(f"   extracted to {new_dir.relative_to(install)} and marked complete")
print(f"   pointer still says {core.read_current()} — activation happens at next launch")

# ---------------------------------------------------------------- 7. the launcher activates it
print("\n7. Launching via the real compiled launcher:")
out = subprocess.run([str(install / "yada"), "toggle"], capture_output=True, text=True,
                     env={**os.environ, "YADA_INSTALL_ROOT": str(install)})
print(out.stdout.rstrip() or out.stderr.rstrip())
print(f"   pointer now: {core.read_current()}")
assert core.read_current() == NEW, "launcher failed to activate the staged version"

# ---------------------------------------------------------------- 8. refuse a tampered release
print("\n8. Tampering with the archive and re-checking…")
(serve / archive_name).write_bytes((serve / archive_name).read_bytes() + b"evil")
shutil.rmtree(install / "versions" / NEW)
svc2 = service.UpdateService(repo="you/yada", current_version=OLD)
status2 = asyncio.run(svc2.check_now())
print(f"   ready    : {status2.ready_version}")
print(f"   error    : {status2.last_error}")
assert status2.ready_version is None, "a tampered archive must not be staged"
assert "checksum" in (status2.last_error or ""), status2.last_error
assert not (install / "versions" / NEW).exists()

httpd.shutdown()
print("\n" + "=" * 74)
print("REHEARSAL PASSED — publish, download, verify, extract, activate, and refuse")
print("=" * 74)
