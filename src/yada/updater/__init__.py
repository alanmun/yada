"""Silent, per-user auto-update from GitHub Releases."""

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
    write_current,
)
from .github import (
    Release,
    UpdateError,
    download_and_verify,
    extract_release,
    fetch_latest,
    update_available,
)
from .service import UpdateService, UpdateStatus

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
    "update_available",
    "write_current",
]
