"""API keys via the OS keyring.

Keys never touch settings.json. `keyring` resolves to Windows Credential Manager and, on
Plasma, KWallet. Env vars are checked first so a shell-exported key works without any
setup -- useful for the spike scripts and for CI.
"""

from __future__ import annotations

import contextlib
import os

SERVICE = "yada"


class KeyringUnavailable(RuntimeError):
    """No usable keyring backend (headless session, or KWallet not running)."""


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
    # keyring's fail/null backend silently discards writes; treat it as unavailable so the
    # UI can say so instead of appearing to save a key that vanishes.
    if backend.__class__.__module__.endswith("fail"):
        raise KeyringUnavailable("no keyring backend available in this session")
    return keyring


def get_key(provider_id: str, env_var: str | None = None) -> str | None:
    if env_var:
        from_env = os.environ.get(env_var)
        if from_env:
            return from_env.strip()
    try:
        return _keyring().get_password(SERVICE, provider_id)
    except KeyringUnavailable:
        return None


def set_key(provider_id: str, key: str) -> None:
    _keyring().set_password(SERVICE, provider_id, key.strip())


def delete_key(provider_id: str) -> None:
    # Deleting a key that was never stored, or with no backend present, is not an error
    # worth surfacing to someone clearing a field in a settings dialog.
    try:
        from keyring.errors import PasswordDeleteError
    except ImportError:
        return
    with contextlib.suppress(KeyringUnavailable, PasswordDeleteError):
        _keyring().delete_password(SERVICE, provider_id)


def keyring_available() -> bool:
    try:
        _keyring()
        return True
    except KeyringUnavailable:
        return False
