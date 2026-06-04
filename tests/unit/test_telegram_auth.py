"""Unit tests for Telegram Mini App initData validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from sreda.config.bot_registry import BotConfig, TelegramBotRegistry
from sreda.services.telegram_auth import (
    TelegramInitDataError,
    TelegramWebAppUser,
    validate_init_data,
    validate_telegram_init_data_any_bot,
)

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def _make_init_data(
    *,
    bot_token: str = BOT_TOKEN,
    user_id: int = 12345,
    first_name: str = "Test",
    username: str = "testuser",
    auth_date: int | None = None,
    extra_params: dict | None = None,
    tamper_hash: str | None = None,
) -> str:
    """Build a valid initData string with correct HMAC signature."""
    if auth_date is None:
        auth_date = int(time.time())

    user_json = json.dumps(
        {"id": user_id, "first_name": first_name, "username": username},
        separators=(",", ":"),
    )

    params: dict[str, str] = {
        "auth_date": str(auth_date),
        "user": user_json,
    }
    if extra_params:
        params.update(extra_params)

    # Build data_check_string
    sorted_pairs = sorted(params.items(), key=lambda p: p[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_pairs)

    # Compute HMAC
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if tamper_hash is not None:
        computed_hash = tamper_hash

    params["hash"] = computed_hash
    return urlencode(params)


class TestValidateInitData:
    def test_valid_signature(self):
        init_data = _make_init_data()
        result = validate_init_data(init_data, BOT_TOKEN)
        assert isinstance(result, TelegramWebAppUser)
        assert result.telegram_id == "12345"
        assert result.first_name == "Test"
        assert result.username == "testuser"

    def test_expired_auth_date(self):
        old_date = int(time.time()) - 7200  # 2 hours ago
        init_data = _make_init_data(auth_date=old_date)
        with pytest.raises(TelegramInitDataError, match="expired"):
            validate_init_data(init_data, BOT_TOKEN, max_age_seconds=3600)

    def test_tampered_hash(self):
        init_data = _make_init_data(tamper_hash="0" * 64)
        with pytest.raises(TelegramInitDataError, match="invalid hash"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_missing_hash(self):
        # Build without hash
        auth_date = str(int(time.time()))
        user_json = json.dumps({"id": 1, "first_name": "A"}, separators=(",", ":"))
        init_data = urlencode({"auth_date": auth_date, "user": user_json})
        with pytest.raises(TelegramInitDataError, match="missing hash"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_empty_init_data(self):
        with pytest.raises(TelegramInitDataError, match="empty"):
            validate_init_data("", BOT_TOKEN)

    def test_wrong_bot_token(self):
        init_data = _make_init_data(bot_token=BOT_TOKEN)
        with pytest.raises(TelegramInitDataError, match="invalid hash"):
            validate_init_data(init_data, "999999:WRONG-TOKEN")

    def test_missing_user_field(self):
        auth_date = str(int(time.time()))
        params = {"auth_date": auth_date}
        sorted_pairs = sorted(params.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_pairs)
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        params["hash"] = h
        init_data = urlencode(params)
        with pytest.raises(TelegramInitDataError, match="missing user"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_custom_max_age(self):
        # auth_date 10 seconds ago, max_age 5 seconds
        old_date = int(time.time()) - 10
        init_data = _make_init_data(auth_date=old_date)
        with pytest.raises(TelegramInitDataError, match="expired"):
            validate_init_data(init_data, BOT_TOKEN, max_age_seconds=5)

    def test_fresh_auth_date_with_short_max_age(self):
        # auth_date now, max_age 60 seconds — should pass
        init_data = _make_init_data(auth_date=int(time.time()))
        result = validate_init_data(init_data, BOT_TOKEN, max_age_seconds=60)
        assert result.telegram_id == "12345"

    def test_extra_params_preserved(self):
        """Extra params in initData do not break validation."""
        init_data = _make_init_data(extra_params={"query_id": "AAHQ1234"})
        result = validate_init_data(init_data, BOT_TOKEN)
        assert result.telegram_id == "12345"


# ---------------------------------------------------------------------------
# Fix 1 security tests: empty-token bots must not be used as HMAC candidates
# ---------------------------------------------------------------------------

def _compute_empty_token_hmac(data_check_string: str) -> str:
    """Produce the HMAC an attacker would compute using an empty-string token."""
    secret_key = hmac.new(b"WebAppData", b"", hashlib.sha256).digest()
    return hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _make_init_data_with_hash(*, user_id: int = 99999, auth_date: int | None = None,
                               forced_hash: str) -> str:
    """Build initData string with a specific (possibly forged) hash."""
    if auth_date is None:
        auth_date = int(time.time())
    user_json = json.dumps({"id": user_id, "first_name": "Attacker"}, separators=(",", ":"))
    params = {"auth_date": str(auth_date), "user": user_json}
    sorted_pairs = sorted(params.items())
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_pairs)
    params["hash"] = forced_hash
    return urlencode(params), data_check_string


class TestEmptyTokenSecurity:
    """Fix 1: validate_telegram_init_data_any_bot must reject empty-token forgeries."""

    def _make_registry(self, *, real_token: str, empty_token: str = "") -> TelegramBotRegistry:
        """Registry with one real-token bot and one empty-token bot."""
        bots = [
            BotConfig(key="sreda", token=real_token, miniapp_shortname="sreda_app"),
            BotConfig(key="empty_bot", token=empty_token, miniapp_shortname="empty_app"),
        ]
        return TelegramBotRegistry(
            bots=bots,
            system_default_bot_key="sreda",
            admin_bot_key="sreda",
        )

    def test_empty_token_forged_initdata_is_rejected(self):
        """An attacker forging initData with the empty-string HMAC must be rejected."""
        real_token = BOT_TOKEN
        registry = self._make_registry(real_token=real_token, empty_token="")

        # Forge initData signed with the empty token
        user_id = 99999
        auth_date = int(time.time())
        user_json = json.dumps({"id": user_id, "first_name": "Attacker"}, separators=(",", ":"))
        params: dict[str, str] = {"auth_date": str(auth_date), "user": user_json}
        sorted_pairs = sorted(params.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_pairs)
        forged_hash = _compute_empty_token_hmac(data_check_string)
        params["hash"] = forged_hash
        init_data = urlencode(params)

        with pytest.raises(TelegramInitDataError, match="invalid hash"):
            validate_telegram_init_data_any_bot(init_data, registry)

    def test_real_token_still_validates_after_fix(self):
        """A legitimately signed initData from the real bot still validates."""
        real_token = BOT_TOKEN
        registry = self._make_registry(real_token=real_token, empty_token="")
        init_data = _make_init_data(bot_token=real_token)
        bot_key, user = validate_telegram_init_data_any_bot(init_data, registry)
        assert bot_key == "sreda"
        assert user.telegram_id == "12345"

    def test_all_empty_tokens_raises_config_error(self):
        """If ALL bots have empty tokens, validation must raise (no silent empty HMAC)."""
        bots = [
            BotConfig(key="bot1", token="", miniapp_shortname="app1"),
            BotConfig(key="bot2", token="", miniapp_shortname="app2"),
        ]
        registry = TelegramBotRegistry(
            bots=bots,
            system_default_bot_key="bot1",
            admin_bot_key="bot1",
        )
        # Any initData should raise — no valid candidates
        init_data = _make_init_data(bot_token="irrelevant")
        with pytest.raises(TelegramInitDataError):
            validate_telegram_init_data_any_bot(init_data, registry)
