"""R-39: вычисление идемпотентных ключей для исполнения инструментов.

Стратегия ключа задаётся в ``ToolContract.idempotency_strategy``:

- ``PER_TURN`` — ``(tenant_id, turn_id, tool_name, action_index)``.
  Защита от parallel hedge: при двойном исполнении того же хода
  второй пройдёт через тот же ключ и dedup отвергнет.
- ``PER_ENTITY`` — ``(tenant_id, tool_name, entity_id)``. Повторное
  удаление того же напоминания — идемпотентно даже между ходами.
- ``NATURAL_KEY`` — ``(tenant_id, tool_name, normalized natural_key_fields)``.
  «Поставь напоминание на 14:00 разбудить Катю», повторённое в двух
  разных сообщениях — собирается в один ключ → один реальный вызов.

Нормализация важна:
- ``title``/строки: ``strip().lower()`` — устраняет регистр и пробелы.
- ``trigger_iso``: парсим datetime, обрезаем до минуты — секундная
  дрожь часов не плодит дубликаты.

Пропущенное обязательное поле → ``MissingIdempotencyField`` (исполнитель
обязан перехватить и пометить запись как FAILURE).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from sreda.agents.contracts import (
    IdempotencyStrategy,
    ToolCall,
    ToolContract,
)


logger = logging.getLogger(__name__)


_FIELD_SEPARATOR = "\x1e"  # record separator — гарантирует уникальное разделение
_KEY_LENGTH = 32           # hex-длина итогового ключа


class MissingIdempotencyField(KeyError):
    """В аргументах вызова нет поля, требуемого стратегией идемпотентности."""


# ─── Главная функция ──────────────────────────────────────────────────


def compute_idempotency_key(
    tenant_id: str,
    turn_id: str,
    contract: ToolContract,
    call: ToolCall,
) -> str:
    """Считает идемпотентный ключ для вызова инструмента.

    Args:
        tenant_id: идентификатор тенанта.
        turn_id: идентификатор хода (нужен только для PER_TURN).
        contract: контракт инструмента (диктует стратегию).
        call: запланированный вызов с аргументами.

    Returns:
        32-символьный hex-ключ (sha256[:16] байт → 32 hex).

    Raises:
        MissingIdempotencyField: ключевое поле отсутствует в args.
    """
    strategy = contract.idempotency_strategy

    if strategy is IdempotencyStrategy.PER_TURN:
        components = (
            "per_turn",
            str(tenant_id),
            turn_id,
            call.tool_name,
            str(call.action_index),
        )
    elif strategy is IdempotencyStrategy.PER_ENTITY:
        field = contract.entity_id_field
        if not field:
            raise MissingIdempotencyField(
                f"{call.tool_name}: PER_ENTITY без entity_id_field в контракте"
            )
        entity_id = call.args.get(field)
        if entity_id is None or entity_id == "":
            raise MissingIdempotencyField(
                f"{call.tool_name}: пропущено обязательное поле {field}"
            )
        components = (
            "per_entity",
            str(tenant_id),
            call.tool_name,
            str(entity_id),
        )
    elif strategy is IdempotencyStrategy.NATURAL_KEY:
        if not contract.natural_key_fields:
            raise MissingIdempotencyField(
                f"{call.tool_name}: NATURAL_KEY без natural_key_fields в контракте"
            )
        normalized: list[str] = []
        for field in contract.natural_key_fields:
            value = call.args.get(field)
            if value is None or value == "":
                raise MissingIdempotencyField(
                    f"{call.tool_name}: пропущено обязательное поле {field}"
                )
            normalized.append(_normalize_for_key(field, value))
        components = (
            "natural_key",
            str(tenant_id),
            call.tool_name,
            *normalized,
        )
    else:
        raise MissingIdempotencyField(f"{call.tool_name}: неизвестная стратегия {strategy!r}")

    raw = _FIELD_SEPARATOR.join(components).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:_KEY_LENGTH]


# ─── Нормализация значений ────────────────────────────────────────────


def _normalize_for_key(field_name: str, value: Any) -> str:
    """Привести значение к канонической форме для хеша.

    Особые случаи:
    - ``trigger_iso``: парсим как datetime, обрезаем до минуты.
    - строки: ``strip().lower()``.
    - всё остальное: ``str()``.
    """
    if field_name == "trigger_iso" and isinstance(value, str):
        return _normalize_iso_to_minute(value)
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0).isoformat()
    if isinstance(value, str):
        return value.strip().lower()
    return str(value)


def _normalize_iso_to_minute(iso_string: str) -> str:
    """ISO datetime → строка вида '2026-05-17T14:00+03:00' (без секунд).

    Если строку не удалось распарсить — оставляем как есть после
    strip/lower. Лучше плохая дедупликация, чем падение.
    """
    raw = iso_string.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # Не падаем (вызывающий получит детерминированный ключ от raw.lower()),
        # но громко жалуемся: это либо planner сбойнул, либо мы получили
        # сырой текст вместо ISO. Без warning'а такой случай маскируется
        # как успешный дедуп и баг не виден.
        logger.warning(
            "idempotency: невозможно разобрать trigger_iso как datetime "
            "(input length=%d) — ключ строится по lowercased raw",
            len(raw),
        )
        return raw.lower()
    dt = dt.replace(second=0, microsecond=0)
    # isoformat() даёт стабильное представление с offset (если был timezone-aware)
    return dt.isoformat()
