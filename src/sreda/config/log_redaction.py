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
from typing import Any


# #366: управляющие символы в co_filename/co_name (dynamically-compiled кадры,
# Codex R1 MINOR) — вырезаем, чтобы кадр не ломал строку лога и не нёс мусор.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def safe_traceback(exc: Any, limit: int = 12) -> str:
    """#366: PII-safe стек прод-краша - цепочка кадров `file:line:func` + типы
    причин, БЕЗ `str(exc)` и значений.

    Зачем не `exc_info=True`: у SQLAlchemy/psycopg `str(exc)` несёт SQL с
    параметрами (текст сообщения пользователя = PII, урок g-039), и финальная
    строка traceback его печатает. Глобальный SecretRedactingFilter НЕ спасает —
    он вырезает только bot-токены, не SQL/ПД. Кадры стека PII НЕ содержат (только
    позиции в коде) → место падения видно безопасно.

    Обход traceback-объектов НАПРЯМУЮ (co_filename/co_name/tb_lineno) - без
    `extract_tb`/`linecache` (не читаем исходники с диска на пути краха, R1 sol).
    Весь тело в try/except → НИКОГДА не роняет вызывающего (это лог-путь страховки;
    исключение отсюда замаскировало бы исходную ошибку хода, R1 все трое)."""
    try:
        if exc is None:
            return ""
        limit = max(1, min(int(limit), 50))
        # кадры: последние `limit`, прямой обход без чтения исходников
        raw: list[str] = []
        tb = getattr(exc, "__traceback__", None)
        while tb is not None:
            code = tb.tb_frame.f_code
            base = code.co_filename.rsplit("/", 1)[-1].rsplit(chr(92), 1)[-1]
            raw.append(_CTRL_RE.sub("?", f"{base}:{tb.tb_lineno}:{code.co_name}")[:120])
            tb = tb.tb_next
        chain = " <- ".join(raw[-limit:])
        # цепочка причин ТОЛЬКО по типам (__cause__ явное, иначе __context__); без
        # сообщений. is not None (не `or` — у exc может быть кастомный __bool__, R1
        # sol/terra). Защита от циклов (seen).
        causes: list[str] = []
        seen = {id(exc)}
        cur = exc.__cause__ if exc.__cause__ is not None else exc.__context__
        while cur is not None and id(cur) not in seen and len(causes) < limit:
            seen.add(id(cur))
            causes.append(_CTRL_RE.sub("?", type(cur).__name__)[:60])
            cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
        tail = (" caused-by=" + ">".join(causes)) if causes else ""
        return redact_secrets(f"{chain}{tail}")
    except Exception:  # noqa: BLE001 — лог-путь НИКОГДА не роняет вызывающего
        return "<traceback-unavailable>"

# Telegram bot-токен: URL-форма bot<id>:<secret> И сырая <id>:<secret>
# (Codex R1: конфиг-ошибки/env могут логировать токен без префикса).
# id — 6+ цифр, secret — base64url-подобный, 20+ символов.
_BOT_TOKEN_RE = re.compile(
    r"bot\d{6,}:[A-Za-z0-9_-]{20,}"          # URL-форма
    r"|(?<![A-Za-z0-9_-])\d{6,}:[A-Za-z0-9_-]{20,}")  # сырая форма
_REDACTED = "bot<redacted>"


def redact_secrets(text: str) -> str:
    """Заменить распознаваемые секреты на маркер. Идемпотентна,
    безопасна на None-проекции (вызывающий приводит к str)."""
    if not text:
        return text
    return _BOT_TOKEN_RE.sub(_REDACTED, text)


class RedactingFormatter(logging.Formatter):
    """Редактирует ФИНАЛЬНУЮ отформатированную строку записи — покрывает
    msg%args, traceback из exc_info и stack_info разом (Codex R1: фильтры
    выполняются ДО форматтера, exc_text строится позже)."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        out = super().format(record)
        try:
            return redact_secrets(out)
        except Exception:  # noqa: BLE001 — форматтер не роняет логи
            return out


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
