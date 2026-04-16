"""Telegram Mini App initData validation.

Implements the server-side verification of ``Telegram.WebApp.initData``
as specified in https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

The ``hash`` field is an HMAC-SHA256 signature derived from the bot token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from sqlalchemy.orm import Session

from sreda.db.models.core import User


class TelegramInitDataError(Exception):
    """Raised when initData validation fails (bad signature, expired, etc.)."""


@dataclass(slots=True)
class TelegramWebAppUser:
    telegram_id: str
    first_name: str | None
    username: str | None


def validate_init_data(
    init_data_raw: str,
    bot_token: str,
    *,
    max_age_seconds: int = 3600,
) -> TelegramWebAppUser:
    """Parse and validate Telegram Mini App ``initData``.

    Returns a ``TelegramWebAppUser`` on success.
    Raises ``TelegramInitDataError`` on any validation failure.
    """
    if not init_data_raw:
        raise TelegramInitDataError("empty initData")

    # 1. Parse as URL query string, preserving order
    pairs = parse_qsl(init_data_raw, keep_blank_values=True)
    if not pairs:
        raise TelegramInitDataError("initData contains no parameters")

    # 2. Extract hash
    received_hash: str | None = None
    data_pairs: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == "hash":
            received_hash = value
        else:
            data_pairs.append((key, value))

    if not received_hash:
        raise TelegramInitDataError("missing hash parameter")

    # 3. Sort remaining pairs alphabetically by key
    data_pairs.sort(key=lambda p: p[0])

    # 4. Build data_check_string: "key=value\nkey=value\n..."
    data_check_string = "\n".join(f"{k}={v}" for k, v in data_pairs)

    # 5. Compute HMAC
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    # 6. Constant-time comparison
    if not hmac.compare_digest(computed_hash, received_hash):
        raise TelegramInitDataError("invalid hash")

    # 7. Check auth_date freshness
    auth_date_str: str | None = None
    user_json: str | None = None
    for key, value in data_pairs:
        if key == "auth_date":
            auth_date_str = value
        elif key == "user":
            user_json = value

    if auth_date_str is None:
        raise TelegramInitDataError("missing auth_date")

    try:
        auth_date = int(auth_date_str)
    except ValueError as exc:
        raise TelegramInitDataError("invalid auth_date") from exc

    age = time.time() - auth_date
    if age > max_age_seconds:
        raise TelegramInitDataError(
            f"initData expired ({int(age)}s > {max_age_seconds}s)"
        )

    # 8. Parse user JSON
    if not user_json:
        raise TelegramInitDataError("missing user field")

    try:
        user_data = json.loads(user_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TelegramInitDataError("invalid user JSON") from exc

    user_id = user_data.get("id")
    if user_id is None:
        raise TelegramInitDataError("user.id missing")

    return TelegramWebAppUser(
        telegram_id=str(user_id),
        first_name=user_data.get("first_name"),
        username=user_data.get("username"),
    )


def resolve_tenant_from_telegram_id(
    session: Session, telegram_id: str
) -> tuple[str, str] | None:
    """Look up tenant_id and user_id by Telegram account ID.

    Returns ``(tenant_id, user_id)`` or ``None`` if no matching user.
    """
    user = (
        session.query(User)
        .filter(User.telegram_account_id == telegram_id)
        .one_or_none()
    )
    if user is None:
        return None
    return user.tenant_id, user.id
