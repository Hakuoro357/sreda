"""AES-256-GCM encryption with versioned ciphertext and key rotation.

Format history:

* **v1** (legacy): ``v1:<base64(nonce)>:<base64(ct)>`` — no key_id.
  Read-only: new writes never produce v1. On decrypt, all known keys
  are tried in order (primary first, then legacy).
* **v2** (current): ``v2:<key_id>:<base64(nonce)>:<base64(ct)>``.
  On decrypt, the exact key identified by ``key_id`` is used; if the
  id is unknown the call fails loudly.

Key derivation:

* If the raw material decodes (base64 or hex) to exactly 32 bytes it
  is used as-is — the operator already has a proper AES-256 key.
* Otherwise the material is treated as a **passphrase** and run through
  ``PBKDF2-HMAC-SHA256`` with 600 000 iterations and a mandatory salt
  from ``SREDA_ENCRYPTION_KEY_SALT``. Missing salt → hard error.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sreda.config.settings import get_settings

_PBKDF2_ITERATIONS = 600_000


class EncryptionConfigError(RuntimeError):
    pass


def encrypt_value(value: str) -> str:
    return get_encryption_service().encrypt(value)


def decrypt_value(value: str) -> str:
    return get_encryption_service().decrypt(value)


class EncryptionService:
    def __init__(
        self,
        *,
        primary_key_id: str,
        keys: dict[str, bytes],
    ) -> None:
        if primary_key_id not in keys:
            raise EncryptionConfigError(
                f"primary key_id {primary_key_id!r} not found in the key map"
            )
        self._primary_key_id = primary_key_id
        self._keys = dict(keys)
        self._ciphers: dict[str, AESGCM] = {
            kid: AESGCM(material) for kid, material in self._keys.items()
        }

    def encrypt(self, value: str) -> str:
        nonce = os.urandom(12)
        cipher = self._ciphers[self._primary_key_id]
        ciphertext = cipher.encrypt(nonce, value.encode("utf-8"), None)
        nonce_b64 = base64.urlsafe_b64encode(nonce).decode("ascii")
        ct_b64 = base64.urlsafe_b64encode(ciphertext).decode("ascii")
        return f"v2:{self._primary_key_id}:{nonce_b64}:{ct_b64}"

    def decrypt(self, value: str) -> str:
        parts = value.split(":", 3)
        version = parts[0]

        if version == "v1":
            return self._decrypt_v1(parts)
        if version == "v2":
            return self._decrypt_v2(parts)
        raise EncryptionConfigError(
            f"Unsupported encryption payload version: {version}"
        )

    # -- private -----------------------------------------------------------

    def _decrypt_v2(self, parts: list[str]) -> str:
        if len(parts) != 4:
            raise EncryptionConfigError("v2 payload has invalid format")
        _, key_id, nonce_b64, ct_b64 = parts
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            raise EncryptionConfigError(
                f"Unknown encryption key_id: {key_id!r}"
            )
        nonce = base64.urlsafe_b64decode(nonce_b64.encode("ascii"))
        ct = base64.urlsafe_b64decode(ct_b64.encode("ascii"))
        return cipher.decrypt(nonce, ct, None).decode("utf-8")

    def _decrypt_v1(self, parts: list[str]) -> str:
        if len(parts) != 3:
            raise EncryptionConfigError("v1 payload has invalid format")
        _, nonce_b64, ct_b64 = parts
        nonce = base64.urlsafe_b64decode(nonce_b64.encode("ascii"))
        ct = base64.urlsafe_b64decode(ct_b64.encode("ascii"))

        # v1 carries no key_id — try all known keys, primary first.
        errors: list[Exception] = []
        for kid in self._key_order():
            try:
                return self._ciphers[kid].decrypt(nonce, ct, None).decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        raise EncryptionConfigError(
            "None of the known keys could decrypt the v1 payload"
        ) from (errors[0] if errors else None)

    def _key_order(self) -> list[str]:
        """Primary first, then the rest in insertion order."""
        order = [self._primary_key_id]
        for kid in self._keys:
            if kid != self._primary_key_id:
                order.append(kid)
        return order


@lru_cache(maxsize=1)
def get_encryption_service() -> EncryptionService:
    settings = get_settings()
    if not settings.encryption_key:
        raise EncryptionConfigError("SREDA_ENCRYPTION_KEY is not configured")

    primary_key_id = settings.encryption_key_id
    primary_material = _normalize_key(
        settings.encryption_key,
        salt=settings.encryption_key_salt,
    )
    keys: dict[str, bytes] = {primary_key_id: primary_material}

    if settings.encryption_legacy_keys:
        try:
            legacy_map = json.loads(settings.encryption_legacy_keys)
        except json.JSONDecodeError as exc:
            raise EncryptionConfigError(
                "SREDA_ENCRYPTION_LEGACY_KEYS is not valid JSON"
            ) from exc
        if not isinstance(legacy_map, dict):
            raise EncryptionConfigError(
                "SREDA_ENCRYPTION_LEGACY_KEYS must be a JSON object"
            )
        for kid, raw in legacy_map.items():
            keys[kid] = _normalize_key(raw, salt=None)

    return EncryptionService(primary_key_id=primary_key_id, keys=keys)


def _normalize_key(raw_key: str, *, salt: str | None) -> bytes:
    text = raw_key.strip()

    # Try base64 (with optional padding fix).
    for candidate in (text, _pad_base64(text)):
        try:
            decoded = base64.urlsafe_b64decode(candidate.encode("ascii"))
        except Exception:
            continue
        if len(decoded) == 32:
            return decoded

    # Try hex.
    if len(text) == 64:
        try:
            decoded = bytes.fromhex(text)
        except ValueError:
            decoded = b""
        if len(decoded) == 32:
            return decoded

    # Passphrase mode — require salt, use PBKDF2.
    if not salt:
        raise EncryptionConfigError(
            "SREDA_ENCRYPTION_KEY looks like a passphrase (not a raw "
            "32-byte key). Set SREDA_ENCRYPTION_KEY_SALT to enable "
            "PBKDF2 key derivation, or provide a proper 32-byte key "
            "in base64/hex."
        )
    return hashlib.pbkdf2_hmac(
        "sha256",
        text.encode("utf-8"),
        salt.encode("utf-8"),
        iterations=_PBKDF2_ITERATIONS,
        dklen=32,
    )


def _pad_base64(value: str) -> str:
    padding = (-len(value)) % 4
    return value + ("=" * padding)
