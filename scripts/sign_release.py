#!/usr/bin/env python
"""Sign a release's SHA256SUMS with the Ed25519 release key.

Run by CI with YADA_SIGNING_KEY in the environment. Writes SHA256SUMS.sig alongside.

Usage:  python scripts/sign_release.py path/to/SHA256SUMS
"""

from __future__ import annotations

import base64
import os
import re
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ROOT = Path(__file__).resolve().parent.parent


def compiled_public_key() -> str:
    """The public key baked into the binaries this release will ship."""
    text = (ROOT / "src" / "yada" / "updater" / "github.py").read_text(encoding="utf-8")
    match = re.search(r'^RELEASE_PUBLIC_KEY_B64 = "([^"]*)"', text, re.MULTILINE)
    return match.group(1) if match else ""


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    manifest = Path(argv[0])
    if not manifest.exists():
        print(f"no such file: {manifest}", file=sys.stderr)
        return 1

    raw = os.environ.get("YADA_SIGNING_KEY", "").strip()
    if not raw:
        print("YADA_SIGNING_KEY is not set.", file=sys.stderr)
        return 1
    try:
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))
    except Exception as exc:  # noqa: BLE001 - a malformed key must fail loudly in CI
        print(f"YADA_SIGNING_KEY is not a valid Ed25519 private key: {exc}", file=sys.stderr)
        return 1

    payload = manifest.read_bytes()
    signature = key.sign(payload)
    out = manifest.with_suffix(manifest.suffix + ".sig")
    out.write_bytes(signature)
    print(f"signed {manifest.name} ({len(payload)} bytes) -> {out.name}")

    # Self-check: proves the signing key is usable at all.
    key.public_key().verify(signature, payload)
    print("signature verifies against its own key")

    # The check that matters: does this signature verify against the public key compiled
    # into the binaries being shipped? If the wrong half of a keypair was pasted into the
    # source, the release publishes fine and then every client refuses to install it --
    # a failure that only surfaces when someone's update silently stops working.
    compiled = compiled_public_key()
    if not compiled:
        print(
            "\nRELEASE_PUBLIC_KEY_B64 is empty in src/yada/updater/github.py.\n"
            "The shipped binaries would have no key to verify against and would refuse\n"
            "every update, including this one. Paste the public half from\n"
            "scripts/gen_signing_key.py, commit, and re-tag.",
            file=sys.stderr,
        )
        return 1
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(compiled)).verify(signature, payload)
    except (InvalidSignature, ValueError) as exc:
        print(
            f"\nThe signing key does not match RELEASE_PUBLIC_KEY_B64 ({compiled[:16]}…).\n"
            f"Every client would refuse this release. Regenerate the pair with\n"
            f"scripts/gen_signing_key.py, update both the YADA_SIGNING_KEY secret and\n"
            f"the constant in src/yada/updater/github.py, then re-tag.\n"
            f"({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return 1
    print(f"signature verifies against the compiled-in public key ({compiled[:16]}…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
