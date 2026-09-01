"""Credential resolution: one place, whatever the launch method.

The behaviour that matters is that a key set once is found again, that the environment
always wins for one-off overrides, and that the fallback file is never world-readable.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from yada import secrets


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the config dir at a temp path and force the no-keyring path."""
    monkeypatch.setattr(secrets, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(secrets, "_keyring", lambda: (_ for _ in ()).throw(
        secrets.KeyringUnavailable("test: no backend")
    ))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return tmp_path


def test_round_trip_via_file_fallback(isolated):
    store = secrets.set_key("openai", "sk-abc123")
    assert store is secrets.Store.FILE
    key, source = secrets.resolve_key("openai", "OPENAI_API_KEY")
    assert key == "sk-abc123"
    assert source is secrets.Store.FILE


def test_file_fallback_is_owner_only(isolated):
    secrets.set_key("openai", "sk-abc123")
    path = secrets.credentials_path()
    assert path.exists()
    if os.name != "nt":
        mode = path.stat().st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO), (
            "credentials must not be group- or world-readable"
        )
        assert not secrets.file_store_is_permissive()


def test_environment_overrides_stored_key(isolated, monkeypatch):
    secrets.set_key("openai", "sk-stored")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    key, source = secrets.resolve_key("openai", "OPENAI_API_KEY")
    assert key == "sk-from-env", "an explicit env var must win for one-off overrides"
    assert source is secrets.Store.ENV


def test_blank_env_var_does_not_mask_stored_key(isolated, monkeypatch):
    secrets.set_key("openai", "sk-stored")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    key, source = secrets.resolve_key("openai", "OPENAI_API_KEY")
    assert key == "sk-stored", "an empty env var is not a credential"
    assert source is secrets.Store.FILE


def test_delete_clears_the_key(isolated):
    secrets.set_key("openai", "sk-abc")
    secrets.delete_key("openai")
    assert secrets.resolve_key("openai", "OPENAI_API_KEY") == (None, secrets.Store.NONE)


def test_multiple_providers_coexist(isolated):
    secrets.set_key("openai", "sk-one")
    secrets.set_key("openrouter", "sk-two")
    assert secrets.get_key("openai") == "sk-one"
    assert secrets.get_key("openrouter") == "sk-two"
    secrets.delete_key("openai")
    assert secrets.get_key("openai") is None
    assert secrets.get_key("openrouter") == "sk-two", "deleting one key must not affect another"


def test_setting_blank_key_clears_it(isolated):
    secrets.set_key("openai", "sk-abc")
    assert secrets.set_key("openai", "   ") is secrets.Store.NONE
    assert secrets.get_key("openai") is None


def test_corrupt_credentials_file_does_not_crash(isolated):
    secrets.credentials_path().write_text("{ not json")
    assert secrets.get_key("openai") is None
    # ...and remains writable afterwards
    assert secrets.set_key("openai", "sk-new") is secrets.Store.FILE
    assert secrets.get_key("openai") == "sk-new"


def test_keys_promote_to_keyring_when_one_appears(isolated, monkeypatch):
    """A key written to the file should move into the keyring once one exists."""
    secrets.set_key("openai", "sk-in-file")
    assert json.loads(secrets.credentials_path().read_text())["openai"] == "sk-in-file"

    vault: dict[tuple[str, str], str] = {}

    class FakeKeyring:
        @staticmethod
        def set_password(service, user, password):
            vault[(service, user)] = password

        @staticmethod
        def get_password(service, user):
            return vault.get((service, user))

    monkeypatch.setattr(secrets, "_keyring", lambda: FakeKeyring)
    assert secrets.set_key("openai", "sk-in-file") is secrets.Store.KEYRING
    assert json.loads(secrets.credentials_path().read_text()) == {}, (
        "promoting to the keyring must leave exactly one source of truth"
    )
    assert secrets.resolve_key("openai") == ("sk-in-file", secrets.Store.KEYRING)
