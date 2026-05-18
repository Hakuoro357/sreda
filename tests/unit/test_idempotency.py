"""R-39: тесты compute_idempotency_key.

Защищает от:
- Дубликата на parallel hedge (PER_TURN)
- Повторного действия на ту же сущность (PER_ENTITY)
- Двух идентичных запросов в разных ходах (NATURAL_KEY) — например
  пользователь дважды сказал «поставь напоминалку на 14:00 разбудить
  Катю».
"""

from __future__ import annotations

import pytest

from sreda.agents.contracts import (
    TOOL_CONTRACTS,
    IdempotencyStrategy,
    ToolCall,
    ToolContract,
    MutationKind,
)
from sreda.agents.idempotency import (
    MissingIdempotencyField,
    compute_idempotency_key,
)


# ─── PER_TURN ─────────────────────────────────────────────────────────


def test_per_turn_stable_for_same_inputs() -> None:
    """Одинаковые tenant+turn+tool+index → одинаковый ключ."""
    contract = TOOL_CONTRACTS["add_shopping_items"]
    call = ToolCall(
        tool_name="add_shopping_items",
        args={"items_summary": "молоко, хлеб"},
        action_index=0,
    )
    k1 = compute_idempotency_key(42, "turn-001", contract, call)
    k2 = compute_idempotency_key(42, "turn-001", contract, call)
    assert k1 == k2


def test_per_turn_ignores_args() -> None:
    """PER_TURN: ключ не зависит от args — только от позиции в плане."""
    contract = TOOL_CONTRACTS["add_shopping_items"]
    a = ToolCall("add_shopping_items", {"items_summary": "молоко"}, 0)
    b = ToolCall("add_shopping_items", {"items_summary": "хлеб"}, 0)
    assert compute_idempotency_key(42, "turn-001", contract, a) == \
        compute_idempotency_key(42, "turn-001", contract, b)


def test_per_turn_varies_with_action_index() -> None:
    """Два вызова в одном ходе на разные индексы → разные ключи."""
    contract = TOOL_CONTRACTS["add_shopping_items"]
    a = ToolCall("add_shopping_items", {"items_summary": "x"}, 0)
    b = ToolCall("add_shopping_items", {"items_summary": "x"}, 1)
    assert compute_idempotency_key(42, "turn-001", contract, a) != \
        compute_idempotency_key(42, "turn-001", contract, b)


def test_per_turn_varies_with_turn_id() -> None:
    contract = TOOL_CONTRACTS["add_shopping_items"]
    call = ToolCall("add_shopping_items", {"items_summary": "x"}, 0)
    k1 = compute_idempotency_key(42, "turn-A", contract, call)
    k2 = compute_idempotency_key(42, "turn-B", contract, call)
    assert k1 != k2


def test_per_turn_varies_with_tenant() -> None:
    contract = TOOL_CONTRACTS["add_shopping_items"]
    call = ToolCall("add_shopping_items", {"items_summary": "x"}, 0)
    k1 = compute_idempotency_key(42, "turn-001", contract, call)
    k2 = compute_idempotency_key(99, "turn-001", contract, call)
    assert k1 != k2


# ─── PER_ENTITY ───────────────────────────────────────────────────────


def test_per_entity_uses_entity_id_field() -> None:
    contract = TOOL_CONTRACTS["cancel_reminder"]
    call = ToolCall("cancel_reminder", {"reminder_id": "rem_42"}, 0)
    key = compute_idempotency_key(42, "turn-001", contract, call)
    assert key  # не пусто, не упало


def test_per_entity_stable_across_turns() -> None:
    """Та же сущность — тот же ключ независимо от turn_id."""
    contract = TOOL_CONTRACTS["cancel_reminder"]
    call = ToolCall("cancel_reminder", {"reminder_id": "rem_42"}, 0)
    k1 = compute_idempotency_key(42, "turn-A", contract, call)
    k2 = compute_idempotency_key(42, "turn-B", contract, call)
    assert k1 == k2


def test_per_entity_varies_with_entity_id() -> None:
    contract = TOOL_CONTRACTS["cancel_reminder"]
    a = ToolCall("cancel_reminder", {"reminder_id": "rem_1"}, 0)
    b = ToolCall("cancel_reminder", {"reminder_id": "rem_2"}, 0)
    assert compute_idempotency_key(42, "turn-001", contract, a) != \
        compute_idempotency_key(42, "turn-001", contract, b)


def test_per_entity_missing_field_raises() -> None:
    contract = TOOL_CONTRACTS["cancel_reminder"]
    call = ToolCall("cancel_reminder", {}, 0)  # нет reminder_id
    with pytest.raises(MissingIdempotencyField, match="reminder_id"):
        compute_idempotency_key(42, "turn-001", contract, call)


# ─── NATURAL_KEY ──────────────────────────────────────────────────────


def test_natural_key_uses_normalized_fields() -> None:
    contract = TOOL_CONTRACTS["schedule_reminder"]
    call = ToolCall(
        "schedule_reminder",
        {"title": "Разбудить Катю", "trigger_iso": "2026-05-17T14:00:00+03:00"},
        0,
    )
    key = compute_idempotency_key(42, "turn-001", contract, call)
    assert key


def test_natural_key_stable_across_turns() -> None:
    """Тот же title+trigger в двух разных turn_id → тот же ключ.

    Защита: пользователь дважды отправил «поставь на 14:00 разбудить
    Катю» — второй раз должен идемпотентно не создать дубликат.
    """
    contract = TOOL_CONTRACTS["schedule_reminder"]
    call = ToolCall(
        "schedule_reminder",
        {"title": "Разбудить Катю", "trigger_iso": "2026-05-17T14:00:00+03:00"},
        0,
    )
    k1 = compute_idempotency_key(42, "turn-A", contract, call)
    k2 = compute_idempotency_key(42, "turn-B", contract, call)
    assert k1 == k2


def test_natural_key_normalizes_case() -> None:
    """«Разбудить» и «разбудить» — один ключ (защита от опечатки регистра)."""
    contract = TOOL_CONTRACTS["schedule_reminder"]
    a = ToolCall(
        "schedule_reminder",
        {"title": "Разбудить Катю", "trigger_iso": "2026-05-17T14:00:00+03:00"},
        0,
    )
    b = ToolCall(
        "schedule_reminder",
        {"title": "разбудить катю", "trigger_iso": "2026-05-17T14:00:00+03:00"},
        0,
    )
    assert compute_idempotency_key(42, "turn-A", contract, a) == \
        compute_idempotency_key(42, "turn-B", contract, b)


def test_natural_key_normalizes_trigger_iso_to_minute() -> None:
    """ISO datetime округляется до минуты — секундная дрожь не плодит дубликаты."""
    contract = TOOL_CONTRACTS["schedule_reminder"]
    a = ToolCall(
        "schedule_reminder",
        {"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
        0,
    )
    b = ToolCall(
        "schedule_reminder",
        {"title": "X", "trigger_iso": "2026-05-17T14:00:45+03:00"},
        0,
    )
    assert compute_idempotency_key(42, "turn-A", contract, a) == \
        compute_idempotency_key(42, "turn-B", contract, b)


def test_natural_key_varies_with_time() -> None:
    contract = TOOL_CONTRACTS["schedule_reminder"]
    a = ToolCall(
        "schedule_reminder",
        {"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
        0,
    )
    b = ToolCall(
        "schedule_reminder",
        {"title": "X", "trigger_iso": "2026-05-17T15:00:00+03:00"},
        0,
    )
    assert compute_idempotency_key(42, "turn-A", contract, a) != \
        compute_idempotency_key(42, "turn-A", contract, b)


def test_natural_key_missing_field_raises() -> None:
    contract = TOOL_CONTRACTS["schedule_reminder"]
    call = ToolCall("schedule_reminder", {"title": "X"}, 0)  # нет trigger_iso
    with pytest.raises(MissingIdempotencyField, match="trigger_iso"):
        compute_idempotency_key(42, "turn-001", contract, call)


def test_natural_key_title_none_raises() -> None:
    """R-39 review MINOR 2: title=None — тоже missing."""
    contract = TOOL_CONTRACTS["schedule_reminder"]
    call = ToolCall(
        "schedule_reminder",
        {"title": None, "trigger_iso": "2026-05-17T14:00:00+03:00"},
        0,
    )
    with pytest.raises(MissingIdempotencyField, match="title"):
        compute_idempotency_key(42, "turn-001", contract, call)


def test_natural_key_title_empty_raises() -> None:
    """R-39 review MINOR 2: пустой title — тоже missing."""
    contract = TOOL_CONTRACTS["schedule_reminder"]
    call = ToolCall(
        "schedule_reminder",
        {"title": "", "trigger_iso": "2026-05-17T14:00:00+03:00"},
        0,
    )
    with pytest.raises(MissingIdempotencyField, match="title"):
        compute_idempotency_key(42, "turn-001", contract, call)


def test_unparseable_trigger_iso_logs_warning(caplog) -> None:
    """R-39 review MAJOR 3: невалидный trigger_iso → warning в лог.

    Без warning сценарий «planner забыл сконвертировать ‘через 2 часа’ в
    ISO» маскируется как нормальная работа с дедупом.
    """
    import logging
    contract = TOOL_CONTRACTS["schedule_reminder"]
    call = ToolCall(
        "schedule_reminder",
        {"title": "X", "trigger_iso": "через 2 часа"},  # не ISO
        0,
    )
    with caplog.at_level(logging.WARNING):
        key = compute_idempotency_key(42, "turn-001", contract, call)
    assert key  # не упало
    assert any("trigger_iso" in rec.message for rec in caplog.records)


# ─── Кати-кейс ────────────────────────────────────────────────────────


def test_kati_correction_makes_different_key_than_original() -> None:
    """В Кати-кейсе первый ход (14:00 ← должно было быть 2 часа) и
    второй (14:00 правильно) — два разных trigger'а после resolve."""
    contract = TOOL_CONTRACTS["schedule_reminder"]
    original = ToolCall(
        "schedule_reminder",
        {"title": "Разбудить", "trigger_iso": "2026-05-17T02:00:00+03:00"},
        0,
    )
    corrected = ToolCall(
        "schedule_reminder",
        {"title": "Разбудить Катю", "trigger_iso": "2026-05-17T14:00:00+03:00"},
        0,
    )
    assert compute_idempotency_key(42, "turn-A", contract, original) != \
        compute_idempotency_key(42, "turn-B", contract, corrected)
