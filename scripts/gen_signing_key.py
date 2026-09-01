#!/usr/bin/env python
"""Generate the release-signing keypair.

Run once. The private key goes into a GitHub Actions secret and nowhere else; the public key
is pasted into `RELEASE_PUBLIC_KEY_B64` in src/yada/updater/github.py and ships inside every
binary.

Why this exists: the updater downloads an archive and then executes it. HTTPS proves the
bytes came from GitHub, which is not the same as proving the maintainer published them -- a
stolen account would otherwise be enough to push code to every install. Signing the checksum
manifest closes that gap.

If the private key is ever lost or exposed: generate a new pair, and understand that already
installed clients will refuse the new releases until they are reinstalled. That is the
correct behaviour, and it is worth knowing before it happens.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    key = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(key.private_bytes_raw()).decode()
    public_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode()

    print("=" * 78)
    print("PRIVATE KEY  — GitHub → Settings → Secrets → Actions → new secret")
    print("               Name: YADA_SIGNING_KEY")
    print("=" * 78)
    print(private_b64)
    print()
    print("=" * 78)
    print("PUBLIC KEY   — paste into src/yada/updater/github.py")
    print('               RELEASE_PUBLIC_KEY_B64 = "..."')
    print("=" * 78)
    print(public_b64)
    print()
    print("Store the private key in the GitHub secret only. Do not commit it, and do not")
    print("keep the only copy in your shell history — this output is all you get.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
