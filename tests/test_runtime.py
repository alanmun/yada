"""Process-wide runtime constraints that must apply before native libraries load."""

from __future__ import annotations

import os
import subprocess
import sys


def test_import_caps_openblas_to_one_thread_even_when_inherited() -> None:
    """A machine-wide OpenBLAS setting must not restore the 32 MB-per-worker arenas.

    This needs a child interpreter: yada is already imported by parts of the test suite,
    while the production requirement is specifically that the setting happens on the
    package's first import, before NumPy initialises OpenBLAS.
    """
    env = os.environ.copy()
    env["OPENBLAS_NUM_THREADS"] = "64"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; import yada; print(os.environ['OPENBLAS_NUM_THREADS'])",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.stdout.strip() == "1"
