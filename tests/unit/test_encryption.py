import base64

import pytest

from sreda.config.settings import get_settings
from sreda.services.encryption import (
    EncryptionConfigError,
    decrypt_value,
    encrypt_value,
    get_encryption_service,
)


def test_encrypt_decrypt_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    get_encryption_service.cache_clear()

    encrypted = encrypt_value("секретное значение")

    assert encrypted != "секретное значение"
    assert decrypt_value(encrypted) == "секретное значение"


def test_encrypt_raises_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SREDA_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    get_encryption_service.cache_clear()

    with pytest.raises(EncryptionConfigError):
        encrypt_value("секретное значение")
