"""Unit tests for the Phase 1 bot registry (plans/second-tg-bot-final.md).

Covers:
- Registry resolves "sreda" (from existing config) and "sreda_home"
  (from new home_bot_* config).
- Unknown bot_key raises KeyError (fail-closed).
- Exact env-name binding: SREDA_HOME_BOT_TOKEN populates the field;
  SREDA_SREDA_HOME_BOT_TOKEN does NOT (would be a double-prefix bug).
- Secret masking: repr(settings) does not expose home_bot_token.
- verify_bot_configs: swapped token (username mismatch) raises ValueError.
- verify_bot_configs: duplicate tokens across registry entries raises ValueError.
- verify_bot_configs: matching tokens and usernames pass without error.
"""

from __future__ import annotations

import pytest

from sreda.config.bot_registry import (
    LEGACY_NULL_BOT_KEY,
    BotConfig,
    TelegramBotRegistry,
    telegram_client_for,
    verify_bot_configs,
)
from sreda.config.settings import Settings, _SECRET_FIELD_NAMES, get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fake tokens — never real credentials; structured like Telegram tokens so
# token-format validators (if any) pass.
_FAKE_SREDA_TOKEN = "111111111:AAFakeTokenForSredaBotTestingOnly"
_FAKE_HOME_TOKEN = "222222222:BBFakeTokenForHomesBotTestingOnly"
_FAKE_OTHER_TOKEN = "333333333:CCFakeTokenForOtherBotTestingOnly"


def _fresh_settings(monkeypatch, **env_vars) -> Settings:
    """Build a fresh (non-cached) Settings instance with the given env vars."""
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return Settings()


def _registry_from_settings(settings: Settings) -> TelegramBotRegistry:
    return TelegramBotRegistry.from_settings(settings)


# ---------------------------------------------------------------------------
# Registry: basic resolution
# ---------------------------------------------------------------------------


def test_registry_resolves_sreda_bot(monkeypatch):
    """Registry built from settings always contains the 'sreda' bot."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=_FAKE_SREDA_TOKEN,
        SREDA_TELEGRAM_BOT_USERNAME="SredaBot",
    )
    registry = _registry_from_settings(settings)
    cfg = registry.resolve("sreda")
    assert cfg.key == "sreda"
    assert cfg.token == _FAKE_SREDA_TOKEN
    assert cfg.username == "SredaBot"


def test_registry_resolves_sreda_home_bot_when_configured(monkeypatch):
    """Registry includes 'sreda_home' when SREDA_HOME_BOT_TOKEN is set."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=_FAKE_SREDA_TOKEN,
        SREDA_HOME_BOT_TOKEN=_FAKE_HOME_TOKEN,
        SREDA_HOME_BOT_USERNAME="SredaHomeBot",
    )
    registry = _registry_from_settings(settings)
    cfg = registry.resolve("sreda_home")
    assert cfg.key == "sreda_home"
    assert cfg.token == _FAKE_HOME_TOKEN
    assert cfg.username == "SredaHomeBot"


def test_registry_sreda_home_absent_when_not_configured(monkeypatch):
    """When SREDA_HOME_BOT_TOKEN is absent, 'sreda_home' is not registered."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=_FAKE_SREDA_TOKEN,
    )
    registry = _registry_from_settings(settings)
    with pytest.raises(KeyError, match="sreda_home"):
        registry.resolve("sreda_home")


def test_registry_unknown_bot_key_raises(monkeypatch):
    """Resolving an unknown bot_key raises KeyError (fail-closed)."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=_FAKE_SREDA_TOKEN,
    )
    registry = _registry_from_settings(settings)
    with pytest.raises(KeyError, match="totally_unknown"):
        registry.resolve("totally_unknown")


def test_registry_unknown_bot_key_error_lists_known_keys(monkeypatch):
    """The KeyError message includes the list of known keys for debuggability."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=_FAKE_SREDA_TOKEN,
    )
    registry = _registry_from_settings(settings)
    with pytest.raises(KeyError) as exc_info:
        registry.resolve("bogus_key")
    assert "sreda" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_legacy_null_bot_key_constant():
    """LEGACY_NULL_BOT_KEY must be the string 'sreda' — never change after deploy."""
    assert LEGACY_NULL_BOT_KEY == "sreda"


def test_registry_default_routing_keys(monkeypatch):
    """system_default_bot_key and admin_bot_key default to 'sreda'."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=_FAKE_SREDA_TOKEN,
    )
    registry = _registry_from_settings(settings)
    assert registry.system_default_bot_key == "sreda"
    assert registry.admin_bot_key == "sreda"


def test_registry_routing_keys_from_env(monkeypatch):
    """system_default_bot_key and admin_bot_key are settable via env."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=_FAKE_SREDA_TOKEN,
        SREDA_HOME_BOT_TOKEN=_FAKE_HOME_TOKEN,
        SREDA_SYSTEM_DEFAULT_BOT_KEY="sreda_home",
        SREDA_ADMIN_BOT_KEY="sreda_home",
    )
    registry = _registry_from_settings(settings)
    assert registry.system_default_bot_key == "sreda_home"
    assert registry.admin_bot_key == "sreda_home"


# ---------------------------------------------------------------------------
# Exact env-name binding
# ---------------------------------------------------------------------------


def test_sreda_home_bot_token_exact_env_name(monkeypatch):
    """SREDA_HOME_BOT_TOKEN (not SREDA_SREDA_HOME_BOT_TOKEN) populates the field.

    This guards against the double-prefix bug: if the field were named
    'sreda_home_bot_token', pydantic-settings would map it to
    SREDA_SREDA_HOME_BOT_TOKEN (prefix + field name), not the intended
    SREDA_HOME_BOT_TOKEN.
    """
    settings = _fresh_settings(
        monkeypatch,
        SREDA_HOME_BOT_TOKEN=_FAKE_HOME_TOKEN,
    )
    assert settings.home_bot_token == _FAKE_HOME_TOKEN


def test_double_prefix_env_name_does_not_populate_field(monkeypatch):
    """SREDA_SREDA_HOME_BOT_TOKEN must NOT populate home_bot_token.

    If it did, the env-name mapping would be wrong (double prefix).
    """
    settings = _fresh_settings(
        monkeypatch,
        SREDA_SREDA_HOME_BOT_TOKEN=_FAKE_HOME_TOKEN,
    )
    # home_bot_token should remain None (the double-prefix name is extra=ignore'd)
    assert settings.home_bot_token is None


def test_sreda_home_bot_username_exact_env_name(monkeypatch):
    """SREDA_HOME_BOT_USERNAME populates home_bot_username."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_HOME_BOT_USERNAME="SredaHomeBot",
    )
    assert settings.home_bot_username == "SredaHomeBot"


def test_sreda_home_miniapp_shortname_exact_env_name(monkeypatch):
    """SREDA_HOME_MINIAPP_SHORTNAME populates home_miniapp_shortname."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_HOME_MINIAPP_SHORTNAME="sreda_home_app",
    )
    assert settings.home_miniapp_shortname == "sreda_home_app"


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------


def test_home_bot_token_in_secret_field_names():
    """home_bot_token must be in _SECRET_FIELD_NAMES (masking set)."""
    assert "home_bot_token" in _SECRET_FIELD_NAMES


def test_home_bot_token_masked_in_repr(monkeypatch):
    """repr(settings) must not expose home_bot_token value."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_HOME_BOT_TOKEN=_FAKE_HOME_TOKEN,
    )
    text = repr(settings)
    assert _FAKE_HOME_TOKEN not in text, (
        "home_bot_token leaked into repr(settings). "
        "Ensure 'home_bot_token' is in _SECRET_FIELD_NAMES."
    )
    assert "home_bot_token='***'" in text


def test_home_bot_token_attribute_access_returns_real_value(monkeypatch):
    """Attribute access settings.home_bot_token returns the real token (not '***').

    Masking is repr-only — callers need the real value to pass to TelegramClient.
    """
    settings = _fresh_settings(
        monkeypatch,
        SREDA_HOME_BOT_TOKEN=_FAKE_HOME_TOKEN,
    )
    assert settings.home_bot_token == _FAKE_HOME_TOKEN


# ---------------------------------------------------------------------------
# BotConfig repr masking
# ---------------------------------------------------------------------------


def test_bot_config_repr_masks_token():
    """BotConfig repr must never expose the token."""
    cfg = BotConfig(key="sreda_home", token=_FAKE_HOME_TOKEN, username="SredaHomeBot")
    text = repr(cfg)
    assert _FAKE_HOME_TOKEN not in text
    assert "token='***'" in text


# ---------------------------------------------------------------------------
# verify_bot_configs: fail-closed token↔bot verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_passes_when_usernames_match():
    """verify_bot_configs succeeds when getMe returns the expected username."""
    registry = TelegramBotRegistry([
        BotConfig(key="sreda", token=_FAKE_SREDA_TOKEN, username="SredaBot"),
    ])

    async def fake_get_me(token: str) -> dict:
        return {"username": "SredaBot", "id": 111}

    # Should not raise
    await verify_bot_configs(registry, get_me=fake_get_me)


@pytest.mark.asyncio
async def test_verify_raises_on_username_mismatch():
    """Swapped token (wrong username from getMe) raises ValueError fail-closed."""
    registry = TelegramBotRegistry([
        BotConfig(key="sreda", token=_FAKE_SREDA_TOKEN, username="SredaBot"),
    ])

    async def fake_get_me(token: str) -> dict:
        # Simulates a swapped token: getMe returns a different bot's username
        return {"username": "WrongBot", "id": 999}

    with pytest.raises(ValueError, match="username"):
        await verify_bot_configs(registry, get_me=fake_get_me)


@pytest.mark.asyncio
async def test_verify_raises_on_duplicate_tokens():
    """Two bots with the same token raises ValueError fail-closed."""
    # TelegramBotRegistry allows distinct keys — we test duplicate token detection
    # in verify_bot_configs.
    registry = TelegramBotRegistry([
        BotConfig(key="sreda", token=_FAKE_SREDA_TOKEN, username="SredaBot"),
        BotConfig(key="sreda_home", token=_FAKE_SREDA_TOKEN, username="SredaHomeBot"),
    ])

    async def fake_get_me(token: str) -> dict:
        return {"username": "SredaBot", "id": 111}

    with pytest.raises(ValueError, match="duplicate"):
        await verify_bot_configs(registry, get_me=fake_get_me)


@pytest.mark.asyncio
async def test_verify_passes_with_two_bots_distinct_tokens():
    """Two bots with distinct tokens and matching usernames: verify passes."""
    registry = TelegramBotRegistry([
        BotConfig(key="sreda", token=_FAKE_SREDA_TOKEN, username="SredaBot"),
        BotConfig(key="sreda_home", token=_FAKE_HOME_TOKEN, username="SredaHomeBot"),
    ])

    async def fake_get_me(token: str) -> dict:
        mapping = {
            _FAKE_SREDA_TOKEN: {"username": "SredaBot", "id": 111},
            _FAKE_HOME_TOKEN: {"username": "SredaHomeBot", "id": 222},
        }
        return mapping[token]

    # Should not raise
    await verify_bot_configs(registry, get_me=fake_get_me)


@pytest.mark.asyncio
async def test_verify_skips_username_check_when_username_not_set():
    """When BotConfig.username is None, verify_bot_configs skips getMe for that bot."""
    called_tokens: list[str] = []

    registry = TelegramBotRegistry([
        BotConfig(key="sreda", token=_FAKE_SREDA_TOKEN, username=None),
    ])

    async def fake_get_me(token: str) -> dict:
        called_tokens.append(token)
        return {"username": "Anything", "id": 111}

    await verify_bot_configs(registry, get_me=fake_get_me)
    # getMe should NOT have been called since username is None
    assert called_tokens == []


@pytest.mark.asyncio
async def test_verify_username_check_is_case_insensitive():
    """Username comparison in verify_bot_configs is case-insensitive."""
    registry = TelegramBotRegistry([
        BotConfig(key="sreda", token=_FAKE_SREDA_TOKEN, username="SredaBot"),
    ])

    async def fake_get_me(token: str) -> dict:
        # Telegram may return usernames in different cases
        return {"username": "sredabot", "id": 111}

    # Should not raise
    await verify_bot_configs(registry, get_me=fake_get_me)


# ---------------------------------------------------------------------------
# telegram_client_for
# ---------------------------------------------------------------------------


def test_telegram_client_for_returns_client_with_correct_token(monkeypatch):
    """telegram_client_for resolves bot_key → TelegramClient with the right token."""
    from sreda.integrations.telegram.client import TelegramClient

    registry = TelegramBotRegistry([
        BotConfig(key="sreda", token=_FAKE_SREDA_TOKEN),
    ])
    client = telegram_client_for("sreda", registry)
    assert isinstance(client, TelegramClient)
    assert client.token == _FAKE_SREDA_TOKEN


def test_telegram_client_for_unknown_key_raises(monkeypatch):
    """telegram_client_for propagates KeyError for unknown bot_key."""
    registry = TelegramBotRegistry([
        BotConfig(key="sreda", token=_FAKE_SREDA_TOKEN),
    ])
    with pytest.raises(KeyError):
        telegram_client_for("no_such_bot", registry)


# ---------------------------------------------------------------------------
# signup_open field
# ---------------------------------------------------------------------------


def test_sreda_home_signup_open_default_true(monkeypatch):
    """home_bot_signup_open defaults to True (open signup for sreda_home)."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_HOME_BOT_TOKEN=_FAKE_HOME_TOKEN,
    )
    assert settings.home_bot_signup_open is True


def test_sreda_home_signup_open_settable_false(monkeypatch):
    """home_bot_signup_open can be set to False via env."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_HOME_BOT_TOKEN=_FAKE_HOME_TOKEN,
        SREDA_HOME_BOT_SIGNUP_OPEN="false",
    )
    assert settings.home_bot_signup_open is False


def test_bot_config_signup_open_propagates_to_registry(monkeypatch):
    """BotConfig.signup_open reflects settings.home_bot_signup_open."""
    settings = _fresh_settings(
        monkeypatch,
        SREDA_TELEGRAM_BOT_TOKEN=_FAKE_SREDA_TOKEN,
        SREDA_HOME_BOT_TOKEN=_FAKE_HOME_TOKEN,
        SREDA_HOME_BOT_SIGNUP_OPEN="false",
    )
    registry = _registry_from_settings(settings)
    cfg = registry.resolve("sreda_home")
    assert cfg.signup_open is False
