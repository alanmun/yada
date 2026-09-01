"""Frozen entry point for the yada application.

PyInstaller runs its entry script as `__main__` with no package context, so a module using
relative imports (`from . import ipc`) cannot be the entry point -- it fails at import with
"attempted relative import with no known parent package". This shim imports the real module
by absolute path instead, which keeps the package internals free of packaging concerns.
"""

import sys

from yada.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
