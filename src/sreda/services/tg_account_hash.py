"""HMAC-SHA256 хеширование telegram chat_id (152-ФЗ обезличивание Часть 1).

Plaintext chat_id — PII (личный идентификатор пользователя в Telegram).
Чтобы выйти из-под 152-ФЗ как «оператор ПДн», в продакшене мы храним
только hash для lookup'ов (`User.tg_account_hash`) и зашифрованный
plaintext (`User.telegram_account_id` через EncryptedString) для отправки
сообщений (worker'ам нужно decrypt'нуть, чтобы вызвать `sendMessage`).

Hash — детерминированный (одна и та же chat_id всегда даёт один hash),
salted (HMAC-SHA256 с per-deployment salt — без salt'а злоумышленник,
получивший дамп БД, мог бы перебрать все Telegram user_id и матчить).

Salt — env-переменная `SREDA_TG_ACCOUNT_SALT`. Генерится один раз при
деплое (`python -c 'import secrets; print(secrets.token_hex(32))'`) и
никогда не меняется без полного backfill (иначе все юзеры станут
«незнакомцами» — lookup по hash перестанет находить их в БД).

Использование:
    >>> from sreda.services.tg_account_hash import hash_tg_account
    >>> hash_tg_account("352612382")
    '8a3f...64-hex...'

Где применяется:
  * `services/onboarding.find_user_by_chat_id` — lookup при входящем
    webhook'е.
  * `services/inbound_messages` — резолв юзера при сохранении сообщения.
  * `services/telegram_auth`, `services/eds_account_verification` —
    верификация по chat_id.
  * Миграция 0027 — backfill hash для существующих 8 тенантов.
"""

from __future__ import annotations

import hashlib
import hmac

from sreda.config.settings import get_settings


def hash_tg_account(chat_id: str | int) -> str:
    """Возвращает HMAC-SHA256 hex digest от ``chat_id`` под salt'ом из настроек.

    Args:
        chat_id: telegram numeric chat_id (str или int — нормализуется до str).

    Returns:
        64-символьная hex строка.

    Raises:
        RuntimeError: если ``SREDA_TG_ACCOUNT_SALT`` не задан в окружении.
    """
    settings = get_settings()
    salt = (settings.tg_account_salt or "").strip()
    if not salt:
        raise RuntimeError(
            "SREDA_TG_ACCOUNT_SALT не задан — обезличивание tg chat_id "
            "не сконфигурировано. Сгенерируй: "
            "`python -c 'import secrets; print(secrets.token_hex(32))'` "
            "и положи в env (launch-sreda.sh на Mac mini)."
        )
    return hmac.new(
        salt.encode("utf-8"),
        str(chat_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
