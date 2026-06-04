"""Phase 7: multi-bot Mini App initData validation tests.

Covers:
- validate_telegram_init_data_any_bot: sreda token → bot_key="sreda"
- validate_telegram_init_data_any_bot: sreda_home token → bot_key="sreda_home"
- validate_telegram_init_data_any_bot: unknown token → TelegramInitDataError
- link builder returns per-bot username/shortname
- allowlist test still passes after removing miniapp.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import pytest


# ---------------------------------------------------------------------------
# Helper: build fake initData signed with a given token
# ---------------------------------------------------------------------------

def _build_fake_init_data(
    *,
    token: str,
    telegram_id: int = 12345678,
    first_name: str = "Test",
    username: str = "testuser",
    auth_date: int | None = None,
    start_param: str | None = None,
) -> str:
    """Build a syntactically-correct Telegram initData string signed with *token*.

    This mirrors the algorithm from the Telegram docs and telegram_auth.py:
    1. Build sorted key=value pairs (exclude 'hash').
    2. Build data_check_string = '\\n'.join(sorted pairs).
    3. secret_key = HMAC-SHA256(b'WebAppData', token).
    4. hash = HMAC-SHA256(secret_key, data_check_string).hexdigest().
    5. URL-encode all values and join with '&hash=<hash>'.

    Only FAKE tokens are used here (never the real ones from env).
    """
    if auth_date is None:
        auth_date = int(time.time())

    user_obj = {"id": telegram_id, "first_name": first_name, "username": username}
    user_json = json.dumps(user_obj, separators=(",", ":"))

    pairs: list[tuple[str, str]] = [
        ("auth_date", str(auth_date)),
        ("user", user_json),
    ]
    if start_param is not None:
        pairs.append(("start_param", start_param))

    pairs.sort(key=lambda p: p[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in pairs)

    secret_key = hmac.new(
        b"WebAppData", token.encode("utf-8"), hashlib.sha256
    ).digest()
    sig = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    parts = [f"{k}={urllib.parse.quote(v, safe='')}" for k, v in pairs]
    parts.append(f"hash={sig}")
    return "&".join(parts)


# ---------------------------------------------------------------------------
# Fake tokens (never real tokens)
# ---------------------------------------------------------------------------

_FAKE_SREDA_TOKEN = "FAKE_SREDA_TOKEN_0000000000000001"
_FAKE_HOME_TOKEN = "FAKE_SREDA_HOME_TOKEN_000000000002"
_FAKE_UNKNOWN_TOKEN = "FAKE_UNKNOWN_TOKEN_9999999999999"


# ---------------------------------------------------------------------------
# Registry fixture
# ---------------------------------------------------------------------------

def _make_registry(*, include_home: bool = True):
    """Build a TelegramBotRegistry with fake tokens."""
    from sreda.config.bot_registry import BotConfig, TelegramBotRegistry

    bots = [
        BotConfig(
            key="sreda",
            token=_FAKE_SREDA_TOKEN,
            username="SredaBot",
            miniapp_shortname="sredaapp",
            signup_open=True,
        ),
    ]
    if include_home:
        bots.append(BotConfig(
            key="sreda_home",
            token=_FAKE_HOME_TOKEN,
            username="SredaHomeBot",
            miniapp_shortname="sredahomeapp",
            signup_open=True,
        ))

    return TelegramBotRegistry(
        bots,
        system_default_bot_key="sreda",
        admin_bot_key="sreda",
    )


# ---------------------------------------------------------------------------
# Tests: validate_telegram_init_data_any_bot
# ---------------------------------------------------------------------------

class TestValidateTelegramInitDataAnyBot:
    """Unit tests for the multi-bot HMAC validator."""

    def test_sreda_token_returns_sreda_key(self):
        """initData signed with sreda's token → bot_key='sreda'."""
        from sreda.services.telegram_auth import validate_telegram_init_data_any_bot

        init_data = _build_fake_init_data(token=_FAKE_SREDA_TOKEN)
        registry = _make_registry()

        bot_key, user = validate_telegram_init_data_any_bot(init_data, registry)

        assert bot_key == "sreda"
        assert user.telegram_id == "12345678"

    def test_sreda_home_token_returns_sreda_home_key(self):
        """initData signed with sreda_home's token → bot_key='sreda_home'."""
        from sreda.services.telegram_auth import validate_telegram_init_data_any_bot

        init_data = _build_fake_init_data(
            token=_FAKE_HOME_TOKEN,
            telegram_id=99999999,
            first_name="Home",
        )
        registry = _make_registry()

        bot_key, user = validate_telegram_init_data_any_bot(init_data, registry)

        assert bot_key == "sreda_home"
        assert user.telegram_id == "99999999"
        assert user.first_name == "Home"

    def test_unknown_token_raises_telegram_init_data_error(self):
        """initData signed with an unknown token → TelegramInitDataError (generic)."""
        from sreda.services.telegram_auth import (
            TelegramInitDataError,
            validate_telegram_init_data_any_bot,
        )

        init_data = _build_fake_init_data(token=_FAKE_UNKNOWN_TOKEN)
        registry = _make_registry()

        with pytest.raises(TelegramInitDataError) as exc_info:
            validate_telegram_init_data_any_bot(init_data, registry)

        # Generic message — must not reveal which bot was tried
        assert "invalid hash" in str(exc_info.value).lower()

    def test_start_param_propagated(self):
        """start_param survives through multi-bot validation."""
        from sreda.services.telegram_auth import validate_telegram_init_data_any_bot

        init_data = _build_fake_init_data(
            token=_FAKE_SREDA_TOKEN,
            start_param="lnk_abc123",
        )
        registry = _make_registry()

        bot_key, user = validate_telegram_init_data_any_bot(init_data, registry)

        assert bot_key == "sreda"
        assert user.start_param == "lnk_abc123"

    def test_empty_init_data_raises(self):
        """Empty string → TelegramInitDataError (structural)."""
        from sreda.services.telegram_auth import (
            TelegramInitDataError,
            validate_telegram_init_data_any_bot,
        )

        registry = _make_registry()
        with pytest.raises(TelegramInitDataError):
            validate_telegram_init_data_any_bot("", registry)

    def test_expired_init_data_raises(self):
        """auth_date in the past → TelegramInitDataError even if HMAC matches."""
        from sreda.services.telegram_auth import (
            TelegramInitDataError,
            validate_telegram_init_data_any_bot,
        )

        stale_auth_date = int(time.time()) - 90000  # >24h ago
        init_data = _build_fake_init_data(
            token=_FAKE_SREDA_TOKEN,
            auth_date=stale_auth_date,
        )
        registry = _make_registry()

        with pytest.raises(TelegramInitDataError) as exc_info:
            validate_telegram_init_data_any_bot(init_data, registry)

        assert "expired" in str(exc_info.value).lower()

    def test_single_bot_registry_still_works(self):
        """Single-bot registry (no sreda_home) accepts sreda-signed initData."""
        from sreda.services.telegram_auth import validate_telegram_init_data_any_bot

        init_data = _build_fake_init_data(token=_FAKE_SREDA_TOKEN)
        registry = _make_registry(include_home=False)

        bot_key, user = validate_telegram_init_data_any_bot(init_data, registry)

        assert bot_key == "sreda"


# ---------------------------------------------------------------------------
# Tests: link builder uses per-bot username/shortname
# ---------------------------------------------------------------------------

class TestLinkBuilderPerBot:
    """channel_linking._build_deep_link uses the right bot username/shortname."""

    def test_sreda_deep_link_uses_sreda_shortname(self):
        from sreda.services.channel_linking import _build_deep_link

        link = _build_deep_link(
            "telegram",
            "tok123",
            tg_bot_username="SredaBot",
            tg_miniapp_shortname="sredaapp",
        )
        assert "SredaBot" in link
        assert "sredaapp" in link
        assert "lnk_tok123" in link

    def test_sreda_home_deep_link_uses_home_shortname(self):
        from sreda.services.channel_linking import _build_deep_link

        link = _build_deep_link(
            "telegram",
            "tok456",
            tg_bot_username="SredaHomeBot",
            tg_miniapp_shortname="sredahomeapp",
        )
        assert "SredaHomeBot" in link
        assert "sredahomeapp" in link
        assert "lnk_tok456" in link

    def test_missing_username_raises(self):
        from sreda.services.channel_linking import _build_deep_link

        with pytest.raises(ValueError, match="tg_bot_username"):
            _build_deep_link(
                "telegram",
                "tok789",
                tg_bot_username=None,
                tg_miniapp_shortname="sredaapp",
            )

    def test_registry_resolve_returns_per_bot_config(self):
        """registry.resolve(bot_key) returns the right username/shortname."""
        registry = _make_registry()

        sreda_cfg = registry.resolve("sreda")
        assert sreda_cfg.username == "SredaBot"
        assert sreda_cfg.miniapp_shortname == "sredaapp"

        home_cfg = registry.resolve("sreda_home")
        assert home_cfg.username == "SredaHomeBot"
        assert home_cfg.miniapp_shortname == "sredahomeapp"


# ---------------------------------------------------------------------------
# Tests: validate_init_data (single-bot) still works after refactor
# ---------------------------------------------------------------------------

class TestValidateInitDataSingleBot:
    """Regression: refactoring internals must not break the original API."""

    def test_valid_init_data(self):
        from sreda.services.telegram_auth import validate_init_data

        init_data = _build_fake_init_data(token=_FAKE_SREDA_TOKEN)
        user = validate_init_data(init_data, _FAKE_SREDA_TOKEN)
        assert user.telegram_id == "12345678"

    def test_wrong_token_raises(self):
        from sreda.services.telegram_auth import TelegramInitDataError, validate_init_data

        init_data = _build_fake_init_data(token=_FAKE_SREDA_TOKEN)
        with pytest.raises(TelegramInitDataError):
            validate_init_data(init_data, _FAKE_HOME_TOKEN)
