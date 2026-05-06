"""Integration test conftest — mirror salt/encryption setup from
tests/unit/conftest.py for tests that touch tg_account_hash.

Без этого `tg_account_hash.hash_tg_account()` raises RuntimeError because
SREDA_TG_ACCOUNT_SALT env var не задан в integration test environment.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch):
    """Provide deterministic test-only secrets that production code
    requires at import / runtime."""
    if not os.environ.get("SREDA_ENCRYPTION_KEY"):
        monkeypatch.setenv(
            "SREDA_ENCRYPTION_KEY",
            "0" * 64,  # 32-byte key in hex
        )
    if not os.environ.get("SREDA_ENCRYPTION_KEY_ID"):
        monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "primary")
    if not os.environ.get("SREDA_TG_ACCOUNT_SALT"):
        monkeypatch.setenv(
            "SREDA_TG_ACCOUNT_SALT",
            "test-salt-for-integration-tests-do-not-use-in-prod",
        )
    # Clear cached settings so env vars take effect.
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
