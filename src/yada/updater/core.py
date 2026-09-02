"""Install layout, version selection, activation and rollback.

Windows cannot overwrite a running executable, so yada never tries. Instead every release
lives in its own directory behind a stable launcher, and "updating" is a pointer flip:

    <install root>/
        yada[.exe]        stable launcher -- shortcuts point here and it never changes
        current           text file holding the active version, e.g. "0.3.1"
        versions/0.3.0/   previous release, retained for rollback
        versions/0.3.1/   active release
        versions/0.3.2/   downloaded, verified and extracted; waiting for next launch
        staging/          partial downloads, safe to delete at any time

Consequences worth stating, because they are the point of the design:

* Activation costs one small file write, so the user never watches an installer.
* Rollback is keeping the previous directory, not reinstalling.
* No admin rights are needed anywhere -- everything is under the user's own profile.
* A half-finished download can never be launched, because activation only ever names a
  fully extracted directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "yada"
# Releases beyond this many are pruned. Two is enough for rollback without hoarding.
KEEP_VERSIONS = 2
# A version that fails to report healthy this many times is presumed broken and skipped.
MAX_LAUNCH_ATTEMPTS = 3


def install_root() -> Path:
    """Where releases live.

    Deliberately not Program Files or /usr: a per-user location is what makes silent,
    admin-free updates possible.
    """
    if override := os.environ.get("YADA_INSTALL_ROOT"):
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / APP_NAME


def versions_dir() -> Path:
    return install_root() / "versions"


def staging_dir() -> Path:
    return install_root() / "staging"


def current_file() -> Path:
    return install_root() / "current"


def state_file() -> Path:
    """Launch bookkeeping: attempt counts and health, used for automatic rollback."""
    return install_root() / "state.json"


def executable_name() -> str:
    return f"{APP_NAME}.exe" if sys.platform == "win32" else APP_NAME


# --------------------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, ...]:
    """Compare release tags without pulling in a dependency.

    Handles a leading 'v' and ignores any pre-release suffix, which is enough for
    'v1.2.3' / '1.2.3' / '1.2.3-beta.1'. Unparseable input sorts lowest rather than
    raising, so a malformed tag on the releases page cannot break update checks.
    """
    core = text.strip().lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for chunk in core.split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts) or (0,)


def is_newer(candidate: str, baseline: str) -> bool:
    return parse_version(candidate) > parse_version(baseline)


@dataclass(frozen=True, slots=True)
class InstalledVersion:
    version: str
    path: Path

    @property
    def executable(self) -> Path:
        return self.path / executable_name()

    @property
    def complete(self) -> bool:
        """Extraction writes this marker last, so its presence proves a usable install."""
        return (self.path / ".complete").exists() and self.executable.exists()


def installed_versions() -> list[InstalledVersion]:
    """Installed releases, newest first.

    Dot-prefixed directories are skipped: extraction builds into `.incoming-<v>-<pid>`
    before renaming into place, and an interrupted one must not be mistaken for a release
    -- it would show up in the settings pane and occupy a retention slot.
    """
    root = versions_dir()
    if not root.is_dir():
        return []
    found = [
        InstalledVersion(p.name, p)
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    ]
    return sorted(found, key=lambda v: parse_version(v.version), reverse=True)


# Prefixes of working directories under versions/. Dot-prefixed so `installed_versions`
# skips them: a leftover must never occupy a retention slot or appear as a real release.
LEFTOVER_PREFIXES = (".incoming-", ".trash-")


def abandoned_extractions() -> list[Path]:
    """Working directories left behind by an install, all safe to delete.

    `.incoming-` is a half-finished extraction. `.trash-` is a version that was replaced
    but could not be deleted at the time, because the copy running from it still had files
    mapped -- so this runs again later, when that process is gone.
    """
    root = versions_dir()
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.is_dir() and p.name.startswith(LEFTOVER_PREFIXES)]


class SwapFailed(OSError):
    """A version directory could not be replaced -- and the existing one is untouched.

    That second half is the guarantee callers rely on when writing the message they show:
    whatever went wrong, the user's working install is still there.
    """


def swap_in(incoming: Path, target: Path) -> None:
    """Move `incoming` to `target`, displacing any existing version directory.

    Replaces an `shutil.rmtree(target)` that cost a user their working install. rmtree
    deletes files one at a time, so when a still-running copy holds `python3.dll` mapped,
    everything else in the directory is already gone by the time Windows refuses. What was
    left behind was a shredded version directory that `current` still pointed at, and an
    app that would not start -- from an install that had been healthy a moment earlier.

    Renaming the old directory aside is all-or-nothing instead. Windows will not let you
    delete a mapped file but will let you rename the directory containing it, so a locked
    file leaves the existing install exactly as it was. It is also how a running copy
    survives being replaced: its open handles follow the directory to its new name.
    """
    aside: Path | None = None
    if target.exists():
        aside = target.parent / f".trash-{target.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            os.rename(target, aside)
        except OSError as exc:
            raise SwapFailed(
                f"could not move the existing {target.name} directory aside ({exc})"
            ) from exc
    try:
        os.replace(incoming, target)
    except OSError as exc:
        if aside is not None:
            # A working old version beats no version at all.
            with contextlib.suppress(OSError):
                os.rename(aside, target)
        raise SwapFailed(f"could not move the new files into {target.name} ({exc})") from exc

    # Best effort: files still mapped by the copy we just displaced cannot be deleted yet.
    # `abandoned_extractions` finds the leftover on a later run, once that process is gone.
    if aside is not None:
        shutil.rmtree(aside, ignore_errors=True)


def read_current() -> str | None:
    try:
        value = current_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def write_current(version: str) -> None:
    """Atomic pointer flip -- this single write is what "applying an update" means."""
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = current_file().with_suffix(".tmp")
    tmp.write_text(version + "\n", encoding="utf-8")
    tmp.replace(current_file())


# --------------------------------------------------------------------------------------
# Health tracking, so a bad release cannot brick the app
# --------------------------------------------------------------------------------------


def _load_state() -> dict:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    install_root().mkdir(parents=True, exist_ok=True)
    tmp = state_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(state_file())


def note_launch_attempt(version: str) -> int:
    state = _load_state()
    versions = state.setdefault("versions", {})
    row = versions.setdefault(version, {"attempts": 0, "healthy": False})
    row["attempts"] = int(row.get("attempts", 0)) + 1
    _save_state(state)
    return row["attempts"]


def mark_healthy(version: str) -> None:
    """Called once the app has actually finished starting up.

    Until this happens a version is only a candidate. Three failed starts and the launcher
    stops choosing it, which turns a crash-on-launch release into an inconvenience rather
    than a support call.
    """
    state = _load_state()
    row = state.setdefault("versions", {}).setdefault(version, {})
    row["healthy"] = True
    row["attempts"] = 0
    _save_state(state)


def is_presumed_broken(version: str) -> bool:
    row = _load_state().get("versions", {}).get(version, {})
    return not row.get("healthy", False) and int(row.get("attempts", 0)) >= MAX_LAUNCH_ATTEMPTS


def select_version_to_launch() -> InstalledVersion | None:
    """Newest complete version that is not presumed broken.

    Runs ahead of `current` on purpose: that is how a background-downloaded release
    activates itself at next launch with no installer step.
    """
    for candidate in installed_versions():
        if candidate.complete and not is_presumed_broken(candidate.version):
            return candidate
    # Everything newer looks broken; fall back to whatever last worked.
    pinned = read_current()
    if pinned:
        for candidate in installed_versions():
            if candidate.version == pinned and candidate.complete:
                return candidate
    return None


def prune_old_versions(keep: int = KEEP_VERSIONS) -> list[str]:
    """Drop old releases, never the running one. Also clears abandoned extractions."""
    running = read_current()
    removed: list[str] = []
    for leftover in abandoned_extractions():
        shutil.rmtree(leftover, ignore_errors=True)
    for stale in installed_versions()[keep:]:
        if stale.version == running:
            continue
        shutil.rmtree(stale.path, ignore_errors=True)
        removed.append(stale.version)
    return removed
