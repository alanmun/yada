#!/usr/bin/env python
"""Verify the released executable embeds every frame of yada's icon, not the bootloader's.

pefile is installed with PyInstaller on Windows; no application dependency is needed.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pefile


def main() -> None:
    icon = (Path(__file__).resolve().parents[1] / "src/yada/assets/icons/yada.ico").read_bytes()
    count = struct.unpack_from("<H", icon, 4)[0]
    expected = set()
    for index in range(count):
        length, offset = struct.unpack_from("<II", icon, 6 + index * 16 + 8)
        expected.add(icon[offset : offset + length])
    with pefile.PE(sys.argv[1]) as executable:
        actual = set()
        for resource in executable.DIRECTORY_ENTRY_RESOURCE.entries:
            if resource.id != pefile.RESOURCE_TYPE["RT_ICON"]:
                continue
            for item in resource.directory.entries:
                for language in item.directory.entries:
                    data = language.data.struct
                    actual.add(executable.get_data(data.OffsetToData, data.Size))
    if actual != expected:
        raise SystemExit(f"Executable icon mismatch: expected {count} yada frames")
    print(f"Verified all {count} yada icon frames in {sys.argv[1]}")


if __name__ == "__main__":
    main()
