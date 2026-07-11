"""MAX Mini App initData validation.

Phase 8 of MAX integration sprint. Параллельно к ``services/telegram_auth.py``.

⚠ ВНИМАНИЕ: точный алгоритм валидации initData в MAX docs не описан
(`dev.max.ru/docs/webapps/bridge` упоминает «hash for verification» без
implementation deltails). Эта реализация использует **TG-pattern**
(HMAC-SHA256 c bot_token как key), который стандартен для большинства
мессенджер-mini-app SDK. Live test в Phase 11 подтвердит/опровергнет
гипотезу. Если алгоритм отличается — fix here, схема endpoints не
меняется.

Поля initData (per docs `dev.max.ru/docs/webapps/bridge`):
- ``query_id`` — session id
- ``auth_date`` — UNIX timestamp
- ``hash`` — verification parameter
- ``user`` — JSON: ``{id, name, username, language, photo}``
- ``chat`` — JSON: ``{id, type: "DIALOG"/"CHAT"/"CHANNEL"}``
- ``start_param`` — deep-link payload (для channel linking — это
  ``lnk_<token>`` после Phase 7 deep-link generation)
- ``ip?`` — optional client IP
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


logger = logging.getLogger(__name__)


class MaxInitDataError(Exception):
    """Raised when MAX initData validation fails."""


@dataclass(slots=True)
class MaxWebAppUser:
    """Subset of MAX initData user/chat info we actually use."""

    max_user_id: str
    max_chat_id: str | None
    first_name: str | None
    username: str | None
    start_param: str | None  # deep-link payload (e.g. "lnk_<token>")


def validate_max_init_data(
    init_data_raw: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
) -> MaxWebAppUser:
    """Validate MAX Mini App ``initData`` and parse user/chat.

    ⚠ Algorithm guess (TG-pattern). Если MAX использует другой scheme,
    `hmac.compare_digest` будет fail для valid payload — fix algorithm
    в Phase 11 live test (сравнение с raw initData от живого mini-app).
    """
    if not init_data_raw:
        raise MaxInitDataError("empty initData")

    pairs = parse_qsl(init_data_raw, keep_blank_values=True)
    if not pairs:
        raise MaxInitDataError("initData contains no parameters")

    received_hash: str | None = None
    data_pairs: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == "hash":
            received_hash = value
        else:
            data_pairs.append((key, value))

    if not received_hash:
        raise MaxInitDataError("missing hash parameter")

    data_pairs.sort(key=lambda p: p[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in data_pairs)

    # TG-pattern: HMAC(magic_string, bot_token) → secret_key, then
    # HMAC(secret_key, data_check_string) → expected hash.
    # Magic string TBD — для TG это "WebAppData", для MAX скорее всего
    # что-то аналогичное. Пробуем "WebAppData" сначала; если не работает,
    # пробуем без префикса (просто bot_token как key).
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        # Try alternative: bot_token прямо как key (без magic prefix)
        computed_alt = hmac.new(
            bot_token.encode("utf-8"),
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(computed_alt, received_hash):
            raise MaxInitDataError(
                "invalid hash (tried TG-style and direct-bot-token; "
                "MAX algorithm may differ — see Phase 11 verify)"
            )

    auth_date_str: str | None = None
    user_json: str | None = None
    chat_json: str | None = None
    start_param: str | None = None
    for key, value in data_pairs:
        if key == "auth_date":
            auth_date_str = value
        elif key == "user":
            user_json = value
        elif key == "chat":
            chat_json = value
        elif key == "start_param":
            start_param = value

    if auth_date_str is None:
        raise MaxInitDataError("missing auth_date")
    try:
        auth_date = int(auth_date_str)
    except ValueError as exc:
        raise MaxInitDataError("invalid auth_date") from exc
    age = time.time() - auth_date
    if age > max_age_seconds:
        raise MaxInitDataError(
            f"initData expired ({int(age)}s > {max_age_seconds}s)"
        )

    if not user_json:
        raise MaxInitDataError("missing user field")
    try:
        user_data = json.loads(user_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MaxInitDataError("invalid user JSON") from exc

    # Codex R1 MAJOR #8: MAX webhook payloads используют ``user.user_id``
    # (probe Phase 0). Mini-app initData может следовать тому же shape.
    # docs не специфицируют — принимаем оба поля для robustness, что бы
    # MAX ни прислал.
    user_id = user_data.get("id") or user_data.get("user_id")
    if user_id is None:
        raise MaxInitDataError("user.id / user.user_id missing")

    chat_id_str: str | None = None
    if chat_json:
        try:
            chat_data = json.loads(chat_json)
            if isinstance(chat_data, dict) and chat_data.get("id") is not None:
                chat_id_str = str(chat_data["id"])
        except (json.JSONDecodeError, TypeError):
            pass  # chat field optional

    return MaxWebAppUser(
        max_user_id=str(user_id),
        max_chat_id=chat_id_str,
        first_name=user_data.get("name") or user_data.get("first_name"),
        username=user_data.get("username"),
        start_param=start_param,
    )


def resolve_tenant_from_max_account_id(
    session,  # type: ignore[no-untyped-def]
    max_account_id: str,
) -> tuple[str, str] | None:
    """Look up ``(tenant_id, user_id)`` by MAX account ID.

    Returns ``None`` if no matching user — caller (mini-app context
    resolver) делает lazy provision через ``ensure_max_user_bundle``.

    Mirror ``services.telegram_auth.resolve_tenant_from_telegram_id`` —
    одинаковая сигнатура для unified channel-agnostic dispatch в
    `miniapp._resolve_miniapp_context`.
    """
    # #138 Ф5-5b: резолв через DEFINER под identity-ролью (privileged_session
    # внутри), не прямым SELECT users — переданный ``session`` не используется.
    # Инертно до флипа DSN. max_account_id НЕ unique → неоднозначность возможна:
    # helper бросает AmbiguousExternalIdentity (зеркалит прежний
    # MultipleResultsFound-путь; вызывающий miniapp его ловит → отказ, не дубль).
    from sreda.services.identity_resolve import resolve_external_identity

    resolved = resolve_external_identity("max", max_account_id)
    if resolved is None:
        return None
    return resolved.tenant_id, resolved.user_id
