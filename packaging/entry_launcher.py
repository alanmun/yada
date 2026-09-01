"""Frozen entry point for the stable launcher shim.

See entry_app.py: PyInstaller cannot use a relative-importing module as its entry script.
"""

import sys

from yada.launcher import main

if __name__ == "__main__":
    sys.exit(main())
