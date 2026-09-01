"""Provider API keys, resolved from one place regardless of how yada was launched.

A key entered once works everywhere: a source checkout, a dev build, and the packaged
executable all read the same stores. That falls out of two choices -- the keyring service
name is constant, and credentials live in the user's config directory rather than inside the
versioned install directory, so updates never disturb them.

Three layers, checked in order:

1. **Environment variable** (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`). Highest priority so a
   one-off override or a CI run needs no setup. Never written to.
2. **OS keyring** -- Windows Credential Manager, KWallet on Plasma. The preferred store,
   and the only one where the key is encrypted at rest by the OS.
3. **A 0600 file** in the config directory. Used only when no keyring backend exists, which
   is the normal state in WSL, over SSH, and in containers.

On the safety of layer 3, plainly: it is a plaintext file readable only by your user
account, the same protection model as `~/.aws/credentials`, `~/.netrc`, `.git-credentials`
and a `.env`. Anything with your user's privileges can read it. Encrypting it with a key
stored beside it would be obfuscation, not security, so yada does not pretend. The UI always
names which store a key came from, so this is never a silent downgrade.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from enum import StrEnum
from pathlib import Path

from .config import config_dir

SERVICE = "yada"


class Store(StrEnum):
    ENV = "environment variable"
    KEYRING = "OS keyring"
    FILE = "config file (0600)"
    NONE = "not set"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


class KeyringUnavailable(RuntimeError):
    """No usable keyring backend (headless session, WSL, or KWallet not running)."""


# --------------------------------------------------------------------------------------
# Layer 2: OS keyring
# --------------------------------------------------------------------------------------


def _keyring():
    try:
        import keyring
        from keyring.errors import NoKeyringError
    except ImportError as exc:  # pragma: no cover
        raise KeyringUnavailable("keyring is not installed") from exc
    try:
        backend = keyring.get_keyring()
    except NoKeyringError as exc:  # pragma: no cover
        raise KeyringUnavailable(str(exc)) from exc
    # keyring's fail/null backend silently discards writes. Treating it as unavailable is
    # what lets the file fallback engage instead of a key appearing to save and vanishing.
    if backend.__class__.__module__.endswith("fail"):
        raise KeyringUnavailable("no keyring backend available in this session")
    return keyring


def keyring_available() -> bool:
    try:
        _keyring()
        return True
    except KeyringUnavailable:
        return False


# --------------------------------------------------------------------------------------
# Layer 3: 0600 file
# --------------------------------------------------------------------------------------


def _read_file_store() -> dict[str, str]:
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _write_file_store(data: dict[str, str]) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    # Create with restrictive permissions from the outset. Writing then chmod-ing would
    # leave a window where the file is world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)
    with contextlib.suppress(OSError, NotImplementedError):
        # Windows ignores POSIX modes; ACLs already restrict %APPDATA% to the user.
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def file_store_is_permissive() -> bool:
    """True when the fallback file is readable by anyone but its owner.

    Surfaced in settings so a bad umask or a copied config does not go unnoticed.
    """
    path = credentials_path()
    if not path.exists() or os.name == "nt":
        return False
    mode = path.stat().st_mode
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def get_key(provider_id: str, env_var: str | None = None) -> str | None:
    return resolve_key(provider_id, env_var)[0]


def resolve_key(provider_id: str, env_var: str | None = None) -> tuple[str | None, Store]:
    """Return (key, which store it came from). The store is shown in the UI."""
    if env_var and (from_env := os.environ.get(env_var)):
        return from_env.strip(), Store.ENV
    try:
        if value := _keyring().get_password(SERVICE, provider_id):
            return value, Store.KEYRING
    except KeyringUnavailable:
        pass
    if value := _read_file_store().get(provider_id):
        return value, Store.FILE
    return None, Store.NONE


def set_key(provider_id: str, key: str) -> Store:
    """Store a key in the best available place and report where it went.

    Prefers the OS keyring. When a key is successfully promoted to the keyring, any older
    copy in the fallback file is removed so there is exactly one source of truth.
    """
    key = key.strip()
    if not key:
        delete_key(provider_id)
        return Store.NONE
    try:
        _keyring().set_password(SERVICE, provider_id, key)
    except KeyringUnavailable:
        data = _read_file_store()
        data[provider_id] = key
        _write_file_store(data)
        return Store.FILE
    except Exception:  # noqa: BLE001 - a locked wallet should not lose the key
        data = _read_file_store()
        data[provider_id] = key
        _write_file_store(data)
        return Store.FILE

    data = _read_file_store()
    if data.pop(provider_id, None) is not None:
        _write_file_store(data)
    return Store.KEYRING


def delete_key(provider_id: str) -> None:
    """Remove a key from every store yada writes to.

    Both stores are cleared regardless of which one currently answers, so clearing a field
    in settings cannot leave a stale credential behind in the other.
    """
    with contextlib.suppress(Exception):
        from keyring.errors import PasswordDeleteError

        with contextlib.suppress(KeyringUnavailable, PasswordDeleteError):
            _keyring().delete_password(SERVICE, provider_id)
    data = _read_file_store()
    if data.pop(provider_id, None) is not None:
        _write_file_store(data)


def describe_store() -> str:
    """One line for the settings pane about where keys are being kept."""
    if keyring_available():
        return "Keys are stored in your OS keyring."
    path = credentials_path()
    warning = (
        "  Warning: this file is readable by other accounts."
        if file_store_is_permissive()
        else ""
    )
    return (
        f"No OS keyring in this session, so keys are stored in {path} "
        f"with owner-only permissions.{warning}"
    )
