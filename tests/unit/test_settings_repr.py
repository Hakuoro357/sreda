"""Tests for ``Settings.__repr_args__`` secret masking.

Background: ``src/sreda/config/settings.py`` overrides
``__repr_args__`` to replace secret-typed fields with ``'***'`` when
``repr(settings)`` / ``str(settings)`` / ``f"{settings}"`` is called.
This protects against accidental log/traceback leaks (e.g.
``logger.info(f"loaded {settings}")``).

These lock-in tests guard against:
* Future refactors that drop a field from ``_SECRET_FIELD_NAMES``
* Future fields named like a secret but not added to the masking list
* Behavioural changes that break ``settings.foo.strip()`` callers
  (i.e. masking must NOT change attribute access — only repr output).
"""

from __future__ import annotations

import pytest

from sreda.config.settings import Settings, _SECRET_FIELD_NAMES, get_settings


def _fresh_settings(monkeypatch, **env):
    """Build a fresh Settings with the given env vars set.

    Mirrors how Settings is constructed in prod (env-driven) without
    polluting the LRU-cached singleton across tests.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return Settings()


# ---------------------------------------------------------------------------
# Core masking behaviour
# ---------------------------------------------------------------------------


def test_repr_masks_known_secret_fields(monkeypatch):
    """All fields in ``_SECRET_FIELD_NAMES`` must show '***' when set."""
    secret_value = "MEGA-SECRET-VALUE-DO-NOT-LEAK-1234567890abcdef"

    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=secret_value,
        SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN=secret_value,
        SREDA_OPENAI_API_KEY=secret_value,
        SREDA_MIMO_API_KEY=secret_value,
        SREDA_OPENROUTER_API_KEY=secret_value,
        SREDA_ENCRYPTION_KEY=secret_value,
        SREDA_ENCRYPTION_KEY_SALT=secret_value,
        SREDA_TG_ACCOUNT_SALT=secret_value,
        SREDA_ADMIN_TOKEN=secret_value,
        SREDA_YANDEX_SPEECHKIT_API_KEY=secret_value,
        SREDA_GROQ_API_KEY=secret_value,
        TAVILY_API_KEY=secret_value,
    )

    text = repr(settings)
    assert secret_value not in text, (
        "Secret value leaked into repr(settings). One of the fields "
        "in _SECRET_FIELD_NAMES failed to mask. Check that the field "
        "name string in _SECRET_FIELD_NAMES exactly matches the "
        "Settings attribute name."
    )
    # Each secret field should appear with ***
    for field_name in (
        "telegram_bot_token",
        "telegram_webhook_secret_token",
        "mimo_api_key",
        "openai_api_key",
        "encryption_key",
        "tg_account_salt",
        "admin_token",
        "groq_api_key",
        "tavily_api_key",
    ):
        assert f"{field_name}='***'" in text, (
            f"Expected `{field_name}='***'` in repr but it was missing. "
            f"Either the field is not in _SECRET_FIELD_NAMES or "
            f"__repr_args__ broke."
        )


def test_repr_does_not_mask_unset_secrets(monkeypatch):
    """Secret field with value=None stays as None — masking is for
    leaked values, not for showing whether config is set. Operators
    must be able to see at a glance which secrets are missing.
    """
    settings = _fresh_settings(monkeypatch)
    # No env set → all secret fields are None
    text = repr(settings)
    # When a secret is None, its repr is `name=None` (not `name='***'`)
    assert "telegram_bot_token=None" in text or "telegram_bot_token=None," in text, (
        "Unset secret field must repr as `=None`, not `='***'`. "
        "Otherwise operators cannot tell unset from masked-but-set."
    )


def test_repr_shows_non_secret_fields_unmasked(monkeypatch):
    """Sanity: non-secret fields (api_host, log_level, etc) appear
    in repr with their actual value. Otherwise __repr_args__ would be
    over-eager and hide everything."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_API_HOST="0.0.0.0",
        SREDA_API_PORT="9999",
        SREDA_LOG_LEVEL="DEBUG",
    )
    text = repr(settings)
    assert "api_host='0.0.0.0'" in text
    assert "api_port=9999" in text
    assert "log_level='DEBUG'" in text


def test_settings_attribute_access_returns_actual_value(monkeypatch):
    """CRITICAL: __repr_args__ override must NOT affect attribute
    access. Code like `settings.mimo_api_key.strip()` must continue
    to work and return the real secret. Otherwise we'd break every
    caller in the codebase.
    """
    secret_value = "real-secret-token-do-not-mask-on-access"
    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=secret_value,
        SREDA_ADMIN_TOKEN=secret_value,
    )

    # Direct attribute access — must return the real value, not '***'
    assert settings.telegram_bot_token == secret_value
    assert settings.admin_token == secret_value


# ---------------------------------------------------------------------------
# Coverage gates — make sure the secret list and the field set agree
# ---------------------------------------------------------------------------


def test_all_secret_field_names_exist_on_settings():
    """Every name in ``_SECRET_FIELD_NAMES`` must be a real Settings
    attribute. Otherwise a typo silently disables masking for an
    intended-secret field. (Pydantic is lenient about extra fields,
    so the typo wouldn't crash on its own.)"""
    settings = Settings()
    for name in _SECRET_FIELD_NAMES:
        assert hasattr(settings, name), (
            f"_SECRET_FIELD_NAMES contains '{name}' but Settings has no "
            f"such attribute. Either the field was renamed/removed and "
            f"the secret list wasn't updated, or there's a typo."
        )


def test_critical_secrets_in_secret_field_names():
    """Lock-in: these specific secrets MUST always be in the masking
    set. If a refactor removes any of them from _SECRET_FIELD_NAMES,
    this test fails loudly.
    """
    must_be_masked = {
        "telegram_bot_token",            # full bot control
        "telegram_webhook_secret_token", # webhook auth
        "encryption_key",                 # 152-ФЗ encryption master
        "tg_account_salt",                # 152-ФЗ HMAC salt
        "admin_token",                    # admin dashboard auth
        "mimo_api_key",                   # paid LLM access
        "openrouter_api_key",             # paid LLM access
        "groq_api_key",                   # paid STT access
        "tavily_api_key",                 # paid search access
    }
    missing = must_be_masked - _SECRET_FIELD_NAMES
    assert not missing, (
        f"Critical secret fields missing from _SECRET_FIELD_NAMES: {missing}. "
        f"These must always be masked in repr; otherwise debug logs / "
        f"tracebacks can leak them."
    )
