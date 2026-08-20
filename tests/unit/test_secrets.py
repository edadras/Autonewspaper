"""Secret storage (§33).

API keys never appear in the source tree or in ``settings.json``; they go to
the platform credential store, or - where there is none - to a machine-bound
encrypted vault. These tests exercise the vault, which is the path that
carries the risk: it is a file, and files get truncated, copied between
machines and edited by hand.
"""

from __future__ import annotations

import json

import pytest

from app.config.secrets import SecretStore, fingerprint, mask


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store with no credential backend, so the vault is always used."""
    monkeypatch.setattr("app.config.secrets._KEYRING_OK", False)
    monkeypatch.setattr("app.config.secrets._DPAPI_OK", False)
    return SecretStore(tmp_path / "vault.bin", allow_env=False)


def test_a_secret_survives_a_restart(store, tmp_path, monkeypatch):
    store.set("openai_api_key", "sk-test-0123456789")

    monkeypatch.setattr("app.config.secrets._KEYRING_OK", False)
    reopened = SecretStore(tmp_path / "vault.bin", allow_env=False)
    assert reopened.get("openai_api_key") == "sk-test-0123456789"


def test_the_key_is_not_stored_in_the_clear(store):
    store.set("openai_api_key", "sk-test-0123456789")

    raw = store.vault_path.read_bytes()
    assert b"sk-test-0123456789" not in raw
    assert b"openai_api_key" not in raw


def test_several_secrets_coexist_and_delete_independently(store):
    store.set("openai_api_key", "one")
    store.set("anthropic_api_key", "two")
    assert store.keys() == ["anthropic_api_key", "openai_api_key"]

    store.delete("openai_api_key")
    assert store.get("openai_api_key") is None
    assert store.get("anthropic_api_key") == "two"


def test_a_tampered_vault_is_rejected_rather_than_decrypted(store):
    store.set("openai_api_key", "sk-test-0123456789")
    raw = bytearray(store.vault_path.read_bytes())
    raw[-40] ^= 0x01  # flip a bit inside the ciphertext
    store.vault_path.write_bytes(bytes(raw))
    store._cache.clear()

    assert store.get("openai_api_key") is None


def test_an_unreadable_vault_is_kept_instead_of_being_overwritten(store, tmp_path):
    """Losing every other key to one bad read would be silent data loss."""
    store.set("openai_api_key", "one")
    store.set("anthropic_api_key", "two")
    store.vault_path.write_bytes(b"not a vault at all")
    store._cache.clear()

    store.set("gemini_api_key", "three")

    damaged = [item for item in tmp_path.iterdir() if ".corrupt-" in item.name]
    assert len(damaged) == 1
    assert damaged[0].read_bytes() == b"not a vault at all"
    assert store.get("gemini_api_key") == "three"


def test_the_environment_is_only_consulted_when_it_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.secrets._KEYRING_OK", False)
    monkeypatch.setattr("app.config.secrets._DPAPI_OK", False)
    monkeypatch.setenv("OPENAI_API_KEY", "from-the-environment")

    assert SecretStore(tmp_path / "a.bin", allow_env=True).get("openai_api_key") == "from-the-environment"
    assert SecretStore(tmp_path / "b.bin", allow_env=False).get("openai_api_key") is None


def test_a_vault_written_before_the_tag_existed_still_opens(store, tmp_path):
    """An earlier build wrote nonce + ciphertext with no authentication tag."""
    import os

    from app.config.secrets import _machine_key, _stream

    plain = json.dumps({"openai_api_key": "legacy"}).encode()
    nonce = os.urandom(16)
    payload = bytes(a ^ b for a, b in zip(plain, _stream(_machine_key(), nonce, len(plain)), strict=True))
    store.vault_path.write_bytes(nonce + payload)

    assert store.get("openai_api_key") == "legacy"


def test_secrets_are_masked_and_fingerprinted_for_logs():
    assert mask(None) == "<unset>"
    assert "0123456789" not in mask("sk-test-0123456789")
    assert mask("short") == "*****"
    assert fingerprint("a") == fingerprint("a") != fingerprint("b")
