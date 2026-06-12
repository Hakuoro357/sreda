"""#95 — defense-in-depth: редактирование секретов в логах и heartbeat.

Пин httpx→WARNING и `raise ... from None` в клиенте закрывают основные
пути утечки bot-токена, но урок g-039 прямо фиксирует: этого мало —
остаются НАШ `logger.exception`, строки heartbeat (`last_error` в БД +
админка) и любой будущий логгер. Глобальный фильтр-редактор на корне
логирования + helper для не-лог-строк закрывают класс целиком.
"""

from __future__ import annotations

import logging
import re

# Telegram bot-токен в URL: bot<id>:<secret>/method
# id — 6+ цифр, secret — base64url-подобный, 20+ символов.
_BOT_TOKEN_RE = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")
_REDACTED = "bot<redacted>"


def redact_secrets(text: str) -> str:
    """Заменить распознаваемые секреты на маркер. Идемпотентна,
    безопасна на None-проекции (вызывающий приводит к str)."""
    if not text:
        return text
    return _BOT_TOKEN_RE.sub(_REDACTED, text)


class SecretRedactingFilter(logging.Filter):
    """Редактирует секреты в УЖЕ отформатированном сообщении записи.

    Работает на `record.getMessage()` (msg % args), подменяя `record.msg`
    на отредактированную строку и очищая args — так редактирование
    охватывает и `logger.warning("...%s", exc)`, и `logger.exception(...)`
    (текст traceback идёт через formatter позже, поэтому отдельно
    редактируем `exc_text`/стек тоже)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
            red = redact_secrets(msg)
            if red != msg:
                record.msg = red
                record.args = None
            if record.exc_text:
                record.exc_text = redact_secrets(record.exc_text)
        except Exception:  # noqa: BLE001 — фильтр НИКОГДА не роняет логи
            pass
        return True
