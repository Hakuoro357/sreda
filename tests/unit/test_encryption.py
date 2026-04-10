import base64
import hashlib
import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sreda.config.settings import get_settings
from sreda.services.encryption import (
    EncryptionConfigError,
    decrypt_value,
    encrypt_value,
    get_encryption_service,
)


def _b64key(material: bytes) -> str:
    return base64.urlsafe_b64encode(material).decode("ascii")


def _clear(monkeypatch):
    """Flush cached singletons so the next call picks up current env."""
    get_settings.cache_clear()
    get_encryption_service.cache_clear()


# --- existing tests (updated to clear H9 vars) ---------------------------


def test_encrypt_decrypt_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", _b64key(b"0123456789abcdef0123456789abcdef"))
    _clear(monkeypatch)

    encrypted = encrypt_value("секретное значение")

    assert encrypted != "секретное значение"
    assert decrypt_value(encrypted) == "секретное значение"


def test_encrypt_raises_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SREDA_ENCRYPTION_KEY", raising=False)
    _clear(monkeypatch)

    with pytest.raises(EncryptionConfigError):
        encrypt_value("секретное значение")


# --- H9: versioned ciphertext, key rotation, KDF -------------------------


def test_new_writes_use_v2_format_with_key_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", _b64key(b"A" * 32))
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "prod_2026")
    _clear(monkeypatch)

    ct = encrypt_value("secret")
    parts = ct.split(":", 3)

    assert parts[0] == "v2"
    assert parts[1] == "prod_2026"
    assert len(parts) == 4
    assert decrypt_value(ct) == "secret"


def test_decrypt_reads_legacy_v1_with_current_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key_material = b"0123456789abcdef0123456789abcdef"
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", _b64key(key_material))
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "new_2026")
    _clear(monkeypatch)

    # Build a v1 payload manually (the old format).
    nonce = os.urandom(12)
    ct = AESGCM(key_material).encrypt(nonce, b"historical", None)
    legacy = f"v1:{base64.urlsafe_b64encode(nonce).decode()}:{base64.urlsafe_b64encode(ct).decode()}"

    assert decrypt_value(legacy) == "historical"


def test_rotation_old_key_in_legacy_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    old_mat = b"A" * 32
    new_mat = b"B" * 32

    # Step 1: encrypt under old key.
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", _b64key(old_mat))
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "old_2025")
    _clear(monkeypatch)
    old_ct = encrypt_value("payload under old key")

    # Step 2: rotate — new primary, old goes to legacy.
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", _b64key(new_mat))
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "new_2026")
    monkeypatch.setenv("SREDA_ENCRYPTION_LEGACY_KEYS", json.dumps({"old_2025": _b64key(old_mat)}))
    _clear(monkeypatch)

    assert decrypt_value(old_ct) == "payload under old key"
    fresh = encrypt_value("fresh")
    assert fresh.startswith("v2:new_2026:")
    assert decrypt_value(fresh) == "fresh"


def test_v1_tried_against_all_keys_after_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    old_mat = b"C" * 32
    new_mat = b"D" * 32

    nonce = os.urandom(12)
    ct = AESGCM(old_mat).encrypt(nonce, b"legacy v1", None)
    legacy = f"v1:{base64.urlsafe_b64encode(nonce).decode()}:{base64.urlsafe_b64encode(ct).decode()}"

    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", _b64key(new_mat))
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "new_only")
    monkeypatch.setenv("SREDA_ENCRYPTION_LEGACY_KEYS", json.dumps({"old_leg": _b64key(old_mat)}))
    _clear(monkeypatch)

    assert decrypt_value(legacy) == "legacy v1"


def test_v2_unknown_key_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", _b64key(b"E" * 32))
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "current")
    _clear(monkeypatch)

    with pytest.raises(EncryptionConfigError):
        decrypt_value("v2:missing_kid:AAAA:BBBB")


def test_passphrase_without_salt_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", "not-a-real-key-just-a-passphrase")
    monkeypatch.delenv("SREDA_ENCRYPTION_KEY_SALT", raising=False)
    _clear(monkeypatch)

    with pytest.raises(EncryptionConfigError, match="(?i)salt"):
        encrypt_value("anything")


def test_passphrase_with_salt_uses_kdf_not_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    passphrase = "correct-horse-battery-staple"
    salt = "tenant-rotation-salt-2026"
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", passphrase)
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_SALT", salt)
    _clear(monkeypatch)

    ct = encrypt_value("secret")
    assert decrypt_value(ct) == "secret"

    # The derived key must NOT match raw sha256 — that would prove
    # the KDF was bypassed.
    service = get_encryption_service()
    derived = service._keys[service._primary_key_id]
    naive = hashlib.sha256(passphrase.encode()).digest()
    assert derived != naive
    assert len(derived) == 32
