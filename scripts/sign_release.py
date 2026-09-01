#!/usr/bin/env python
"""Sign a release's SHA256SUMS with the Ed25519 release key.

Run by CI with YADA_SIGNING_KEY in the environment. Writes SHA256SUMS.sig alongside.

Usage:  python scripts/sign_release.py path/to/SHA256SUMS
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


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

    # Verify immediately: a release published with an unverifiable signature would be
    # rejected by every client, and CI is the cheapest place to catch that.
    key.public_key().verify(signature, payload)
    print("signature verifies against the public key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
