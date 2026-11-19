"""Use the host's ALSA library rather than the one the packaged build carries.

The Linux archive is built on Ubuntu and PyInstaller bundles libasound alongside
PortAudio. ALSA is not self-contained: the "default" device is whatever the host's
configuration says, and on a PipeWire or PulseAudio desktop that is a plugin loaded from
the host's plugin directory. A bundled libasound looks for plugins where its *build* distro
keeps them (/usr/lib/x86_64-linux-gnu/alsa-lib), and a host plugin can also demand a newer
libasound than the bundled one. On Fedora both fail, the "default" device does not exist,
and PortAudio's fallback default is the first hardware device: the motherboard's analog
jack, usually with nothing plugged in. "System default" then records silence.

The dynamic loader resolves a dependency by soname against what is already loaded, so
loading the host's libasound.so.2 first makes PortAudio bind to it. The bundled copy stays
as the fallback for a host that has none.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

SONAME = "libasound.so.2"
# Absolute paths on purpose: a frozen build puts its own directory first on the library
# search path, so asking for the bare soname would return the bundled copy.
_HOST_DIRS = (
    "/usr/lib64",
    "/lib64",
    "/usr/lib/x86_64-linux-gnu",
    "/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/lib/aarch64-linux-gnu",
    "/usr/lib",
    "/lib",
)
_done = False


def prefer_host_alsa(*, frozen: bool | None = None, dirs: tuple[str, ...] = _HOST_DIRS) -> str:
    """Load the host's libasound before PortAudio does. Returns the path used, or "".

    Only in a packaged build: from source, PortAudio already resolves to the host library.
    Must run before `import sounddevice`. Safe to call repeatedly.
    """
    global _done
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if _done or not frozen or not sys.platform.startswith("linux"):
        return ""
    _done = True
    for directory in dirs:
        candidate = Path(directory) / SONAME
        if not candidate.exists():
            continue
        try:
            # A library of the wrong architecture (32-bit in /usr/lib) raises; move on.
            ctypes.CDLL(str(candidate), mode=ctypes.RTLD_GLOBAL)
        except OSError:
            continue
        return str(candidate)
    return ""
