from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sreda.config.settings import get_settings


class EncryptionConfigError(RuntimeError):
    pass


def encrypt_value(value: str) -> str:
    service = get_encryption_service()
    return service.encrypt(value)


def decrypt_value(value: str) -> str:
    service = get_encryption_service()
    return service.decrypt(value)


class EncryptionService:
    def __init__(self, raw_key: str) -> None:
        self._key = _normalize_key(raw_key)
        self._cipher = AESGCM(self._key)

    def encrypt(self, value: str) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, value.encode("utf-8"), None)
        nonce_b64 = base64.urlsafe_b64encode(nonce).decode("ascii")
        ciphertext_b64 = base64.urlsafe_b64encode(ciphertext).decode("ascii")
        return f"v1:{nonce_b64}:{ciphertext_b64}"

    def decrypt(self, value: str) -> str:
        try:
            version, nonce_b64, ciphertext_b64 = value.split(":", 2)
        except ValueError as exc:
            raise EncryptionConfigError("Encrypted payload has invalid format") from exc
        if version != "v1":
            raise EncryptionConfigError(f"Unsupported encryption payload version: {version}")
        nonce = base64.urlsafe_b64decode(nonce_b64.encode("ascii"))
        ciphertext = base64.urlsafe_b64decode(ciphertext_b64.encode("ascii"))
        plaintext = self._cipher.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")


@lru_cache(maxsize=1)
def get_encryption_service() -> EncryptionService:
    settings = get_settings()
    if not settings.encryption_key:
        raise EncryptionConfigError("SREDA_ENCRYPTION_KEY is not configured")
    return EncryptionService(settings.encryption_key)


def _normalize_key(raw_key: str) -> bytes:
    text = raw_key.strip()

    for candidate in (text, _pad_base64(text)):
        try:
            decoded = base64.urlsafe_b64decode(candidate.encode("ascii"))
        except Exception:
            continue
        if len(decoded) == 32:
            return decoded

    if len(text) == 64:
        try:
            decoded = bytes.fromhex(text)
        except ValueError:
            decoded = b""
        if len(decoded) == 32:
            return decoded

    return hashlib.sha256(text.encode("utf-8")).digest()


def _pad_base64(value: str) -> str:
    padding = (-len(value)) % 4
    return value + ("=" * padding)
