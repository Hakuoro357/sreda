"""Shared pytest fixtures for unit tests.

Provides a deterministic default ``SREDA_ENCRYPTION_KEY`` so tests that
touch encrypted columns (``AssistantMemory.content``,
``TenantUserSkillConfig.skill_params_json``, etc.) don't have to set the
env var themselves. Tests that need a specific key value (encryption
rotation cases, etc.) use ``monkeypatch.setenv`` inside the test and
override this default.
"""

from __future__ import annotations

import base64
import os

import pytest


_DEFAULT_TEST_KEY = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef"
).decode("ascii")


@pytest.fixture(autouse=True)
def _default_encryption_key(monkeypatch):
    """Set a stable dummy AES-256 key for the whole test suite.

    Required because encrypted ORM columns (``EncryptedString``) call
    into ``services.encryption.get_encryption_service`` on every
    read/write — without a configured key the call raises
    ``EncryptionConfigError`` and the test crashes long before the
    assertion.

    ``monkeypatch`` makes the env var live only for the duration of one
    test, so parallelism and teardown stay clean. The fixture uses
    ``setdefault`` semantics — tests that already set their own key
    through their own monkeypatch calls win (pytest applies their
    patches AFTER this fixture yields).
    """
    # Only inject if not already set by an outer fixture / env.
    if not os.environ.get("SREDA_ENCRYPTION_KEY"):
        monkeypatch.setenv("SREDA_ENCRYPTION_KEY", _DEFAULT_TEST_KEY)
    # Same for the key_id — default matches the value used in tests
    # that historically hard-coded "primary".
    if not os.environ.get("SREDA_ENCRYPTION_KEY_ID"):
        monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "primary")

    # Clear the LRU-cached EncryptionService / Settings so the new env
    # vars take effect for this test's session.
    from sreda.config.settings import get_settings
    from sreda.services.encryption import get_encryption_service

    get_settings.cache_clear()
    get_encryption_service.cache_clear()
    yield
    get_settings.cache_clear()
    get_encryption_service.cache_clear()
