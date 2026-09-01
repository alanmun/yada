"""Silent, per-user auto-update from GitHub Releases.

Imports here are deliberately lazy. `core` is pure standard library -- pointer files, version
comparison, health tracking -- while `github` and `service` need httpx and cryptography. The
stable launcher imports only `core`, and its bundle deliberately excludes those heavier
dependencies, so eagerly importing them here would break the launcher binary at startup.

That is not hypothetical: it is exactly what happened the first time this was built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .core import (
    InstalledVersion,
    install_root,
    installed_versions,
    is_newer,
    mark_healthy,
    note_launch_attempt,
    parse_version,
    prune_old_versions,
    read_current,
    select_version_to_launch,
    staging_dir,
    versions_dir,
    write_current,
)

if TYPE_CHECKING:  # pragma: no cover
    from .github import (
        Release,
        UpdateError,
        download_and_verify,
        extract_release,
        fetch_latest,
        update_available,
    )
    from .service import UpdateService, UpdateStatus

_LAZY = {
    "Release": ".github",
    "UpdateError": ".github",
    "download_and_verify": ".github",
    "extract_release": ".github",
    "fetch_latest": ".github",
    "update_available": ".github",
    "UpdateService": ".service",
    "UpdateStatus": ".service",
}

__all__ = [
    "InstalledVersion",
    "Release",
    "UpdateError",
    "UpdateService",
    "UpdateStatus",
    "download_and_verify",
    "extract_release",
    "fetch_latest",
    "install_root",
    "installed_versions",
    "is_newer",
    "mark_healthy",
    "note_launch_attempt",
    "parse_version",
    "prune_old_versions",
    "read_current",
    "select_version_to_launch",
    "staging_dir",
    "update_available",
    "versions_dir",
    "write_current",
]


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute access, so importing `core` never drags in httpx."""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache, so this costs one lookup per process
    return value
