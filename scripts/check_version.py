#!/usr/bin/env python
"""Verify the git tag, pyproject version and __version__ all agree.

Cheap insurance against a confusing class of bug: yada compares the running version against
the latest release tag to decide whether to update. If a tag says 0.2.0 but the source still
says 0.1.0, every installed copy re-downloads the same release forever, or refuses an update
it should take. Catching that in CI costs a second; catching it in the wild costs a release.

Usage:  python scripts/check_version.py 0.2.0
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def source_version() -> str:
    text = (ROOT / "src" / "yada" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("could not find __version__ in src/yada/__init__.py")
    return match.group(1)


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    tag = argv[0].lstrip("vV")
    src, proj = source_version(), project_version()

    print(f"tag:              {tag}")
    print(f"__version__:      {src}")
    print(f"pyproject:        {proj}")

    if tag == src == proj:
        print("\nAll three agree.")
        return 0
    print(
        "\nThese must all match before tagging. Update src/yada/__init__.py and "
        "pyproject.toml, commit, then re-tag."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
