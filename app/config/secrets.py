"""Secure storage for API keys and other secrets.

Secrets never live in the source tree and never live in ``settings.json``.
The resolution order is:

1. ``keyring`` (Windows Credential Manager / macOS Keychain / SecretService).
2. An encrypted local vault (Windows DPAPI when available, otherwise a
   machine-bound Fernet-less XOR-with-PBKDF2 stream, which is only used on
   platforms without a credential store - i.e. developer machines).
3. Environment variables / ``.env`` - development convenience only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
from pathlib import Path

log = logging.getLogger(__name__)

SERVICE_NAME = "AINewspaperStudio"

try:  # pragma: no cover - depends on platform
    import keyring  # type: ignore

    _KEYRING_OK = True
except Exception:  # pragma: no cover
    keyring = None  # type: ignore
    _KEYRING_OK = False

try:  # pragma: no cover - Windows only
    import win32crypt  # type: ignore

    _DPAPI_OK = True
except Exception:  # pragma: no cover
    win32crypt = None  # type: ignore
    _DPAPI_OK = False


def _machine_key() -> bytes:
    """Derive a machine-bound key used by the portable vault fallback."""
    seed = "|".join(
        [
            platform.node(),
            platform.machine(),
            os.environ.get("USERNAME") or os.environ.get("USER") or "user",
            SERVICE_NAME,
        ]
    ).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", seed, b"ains-vault-v1", 120_000, dklen=32)


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 based keystream (CTR mode) for the portable vault."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


class SecretStore:
    """Facade over the platform credential store with a portable fallback.

    Parameters
    ----------
    vault_path:
        Location of the encrypted fallback vault. Only used when no OS
        credential store is available.
    allow_env:
        When ``True`` (default) environment variables are consulted as a last
        resort so developers can run the app with ``OPENAI_API_KEY`` exported.
    """

    def __init__(self, vault_path: Path, allow_env: bool = True) -> None:
        self.vault_path = Path(vault_path)
        self.allow_env = allow_env
        self._cache: dict[str, str] = {}

    # ------------------------------------------------------------------ API
    def backend_name(self) -> str:
        """Human readable name of the active backend (for diagnostics)."""
        if _KEYRING_OK:
            try:
                return f"keyring:{keyring.get_keyring().__class__.__name__}"  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                pass
        if _DPAPI_OK:
            return "windows-dpapi-vault"
        return "portable-vault"

    def set(self, key: str, value: str) -> None:
        """Store *value* under *key*."""
        self._cache[key] = value
        if _KEYRING_OK:
            try:
                keyring.set_password(SERVICE_NAME, key, value)  # type: ignore[union-attr]
                return
            except Exception as exc:  # pragma: no cover
                log.warning("keyring unavailable (%s); falling back to local vault", exc)
        self._vault_write(key, value)

    def get(self, key: str) -> str | None:
        """Return the secret stored under *key*, or ``None``."""
        if key in self._cache:
            return self._cache[key]
        if _KEYRING_OK:
            try:
                value = keyring.get_password(SERVICE_NAME, key)  # type: ignore[union-attr]
                if value:
                    self._cache[key] = value
                    return value
            except Exception as exc:  # pragma: no cover
                log.debug("keyring read failed: %s", exc)
        value = self._vault_read(key)
        if value:
            self._cache[key] = value
            return value
        if self.allow_env:
            env_value = os.environ.get(key) or os.environ.get(key.upper())
            if env_value:
                return env_value
        return None

    def delete(self, key: str) -> None:
        """Remove the secret stored under *key* from every backend."""
        self._cache.pop(key, None)
        if _KEYRING_OK:
            try:
                keyring.delete_password(SERVICE_NAME, key)  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                pass
        data = self._vault_load()
        if key in data:
            del data[key]
            self._vault_save(data)

    def has(self, key: str) -> bool:
        """Return ``True`` when a non-empty secret exists for *key*."""
        return bool(self.get(key))

    def keys(self) -> list[str]:
        """List the keys held in the local vault (keyring is not enumerable)."""
        return sorted(self._vault_load().keys())

    # ---------------------------------------------------------------- vault
    def _vault_load(self) -> dict[str, str]:
        if not self.vault_path.exists():
            return {}
        try:
            raw = self.vault_path.read_bytes()
            if _DPAPI_OK:  # pragma: no cover - Windows only
                plain = win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1]  # type: ignore[union-attr]
            else:
                nonce, payload = raw[:16], raw[16:]
                plain = bytes(
                    a ^ b for a, b in zip(payload, _stream(_machine_key(), nonce, len(payload)))
                )
            return json.loads(plain.decode("utf-8"))
        except Exception as exc:
            log.error("secret vault unreadable (%s); starting empty", exc)
            return {}

    def _vault_save(self, data: dict[str, str]) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        plain = json.dumps(data).encode("utf-8")
        if _DPAPI_OK:  # pragma: no cover - Windows only
            blob = win32crypt.CryptProtectData(plain, SERVICE_NAME, None, None, None, 0)  # type: ignore[union-attr]
        else:
            nonce = os.urandom(16)
            blob = nonce + bytes(a ^ b for a, b in zip(plain, _stream(_machine_key(), nonce, len(plain))))
        tmp = self.vault_path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(self.vault_path)
        try:
            os.chmod(self.vault_path, 0o600)
        except OSError:  # pragma: no cover
            pass

    def _vault_write(self, key: str, value: str) -> None:
        data = self._vault_load()
        data[key] = value
        self._vault_save(data)

    def _vault_read(self, key: str) -> str | None:
        return self._vault_load().get(key)


def mask(secret: str | None) -> str:
    """Return a log-safe representation of *secret*."""
    if not secret:
        return "<unset>"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-2:]} ({len(secret)} chars)"


def fingerprint(secret: str | None) -> str:
    """Stable non-reversible fingerprint, useful to compare keys in logs."""
    if not secret:
        return "-"
    return base64.b32encode(hashlib.sha256(secret.encode()).digest())[:8].decode()
