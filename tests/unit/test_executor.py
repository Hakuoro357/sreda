"""R-39: тесты исполнителя плана действий."""

from __future__ import annotations

import pytest

from sreda.agents.contracts import (
    ExecutionPlan,
    ResultKind,
    ToolCall,
)
from sreda.agents.executor import execute_plan
from sreda.agents.journal import ToolJournal


# ─── Тестовые callable'ы ──────────────────────────────────────────────


def _ok_schedule(title: str, trigger_iso: str) -> dict:
    return {"reminder_id": f"rem_{hash(title) % 1000:03d}", "title": title}


def _ok_cancel(reminder_id: str) -> dict:
    return {"reminder_id": reminder_id, "title": "Разбудить"}


def _ok_save_recipe(title: str) -> dict:
    return {"recipe_id": f"rec_{hash(title) % 1000:03d}", "title": title}


def _failing_callable(**kwargs) -> None:
    raise RuntimeError("симулируем падение инструмента")


# ─── Пустой/тривиальный план ──────────────────────────────────────────


def test_empty_plan_yields_empty_journal() -> None:
    plan = ExecutionPlan()
    result = execute_plan(
        plan,
        tenant_id=42,
        turn_id="t-001",
        tool_callables={},
    )
    assert result.journal.is_empty
    assert not result.halted_early


def test_single_success_call_recorded() -> None:
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "Разбудить Катю", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=0,
        ),
    ))
    result = execute_plan(
        plan,
        tenant_id=42,
        turn_id="t-001",
        tool_callables={"schedule_reminder": _ok_schedule},
    )
    assert len(result.journal) == 1
    entry = result.journal.entries[0]
    assert entry.result_kind is ResultKind.SUCCESS
    assert entry.tool_name == "schedule_reminder"
    assert entry.idempotency_key is not None
    assert entry.result_data["title"] == "Разбудить Катю"


# ─── Ошибки ───────────────────────────────────────────────────────────


def test_unknown_tool_records_failure() -> None:
    plan = ExecutionPlan(calls=(
        ToolCall(tool_name="some_unknown", args={}, action_index=0),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001", tool_callables={},
    )
    assert len(result.journal) == 1
    e = result.journal.entries[0]
    assert e.result_kind is ResultKind.FAILURE
    assert "unknown_tool" in (e.error_message or "")


def test_missing_idempotency_field_records_failure() -> None:
    """schedule_reminder требует title и trigger_iso для NATURAL_KEY."""
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "X"},  # нет trigger_iso
            action_index=0,
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={"schedule_reminder": _ok_schedule},
    )
    assert len(result.journal) == 1
    e = result.journal.entries[0]
    assert e.result_kind is ResultKind.FAILURE
    assert "missing_idempotency_field" in (e.error_message or "")


def test_no_callable_registered_records_failure() -> None:
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=0,
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001", tool_callables={},
    )
    assert len(result.journal) == 1
    e = result.journal.entries[0]
    assert e.result_kind is ResultKind.FAILURE
    assert "no_callable_registered" in (e.error_message or "")


def test_callable_exception_becomes_failure() -> None:
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=0,
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={"schedule_reminder": _failing_callable},
    )
    assert len(result.journal) == 1
    e = result.journal.entries[0]
    assert e.result_kind is ResultKind.FAILURE
    assert "RuntimeError" in (e.error_message or "")


# ─── Fail-fast и fail-open ────────────────────────────────────────────


def test_fail_fast_halts_on_first_failure() -> None:
    """По умолчанию первый FAIL останавливает план."""
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=0,
        ),
        ToolCall(
            tool_name="save_recipe",
            args={"title": "Борщ"},
            action_index=1,
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={
            "schedule_reminder": _failing_callable,
            "save_recipe": _ok_save_recipe,
        },
    )
    assert result.halted_early
    assert len(result.journal) == 1
    assert result.journal.entries[0].tool_name == "schedule_reminder"
    assert result.journal.entries[0].result_kind is ResultKind.FAILURE


def test_fail_open_continues_after_failure() -> None:
    """fail_fast=False продолжает выполнение."""
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=0,
        ),
        ToolCall(
            tool_name="save_recipe",
            args={"title": "Борщ"},
            action_index=1,
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={
            "schedule_reminder": _failing_callable,
            "save_recipe": _ok_save_recipe,
        },
        fail_fast=False,
    )
    assert not result.halted_early
    assert len(result.journal) == 2
    assert result.journal.entries[0].result_kind is ResultKind.FAILURE
    assert result.journal.entries[1].result_kind is ResultKind.SUCCESS


# ─── Multi-call cancel + schedule (Кати-сценарий) ────────────────────


def test_multi_call_cancel_then_schedule() -> None:
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="cancel_reminder",
            args={"reminder_id": "rem_old"},
            action_index=0,
        ),
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "Разбудить Катю", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=1,
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-kati",
        tool_callables={
            "cancel_reminder": _ok_cancel,
            "schedule_reminder": _ok_schedule,
        },
    )
    assert len(result.journal) == 2
    assert result.journal.all_succeeded
    assert result.journal.entries[0].tool_name == "cancel_reminder"
    assert result.journal.entries[1].tool_name == "schedule_reminder"


# ─── Dedup в plan-е ───────────────────────────────────────────────────


def test_duplicate_call_in_plan_dedupliated() -> None:
    """Две идентичные команды в одном плане → второй пропущен."""
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=0,
        ),
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=0,  # тот же index → тот же ключ
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={"schedule_reminder": _ok_schedule},
    )
    assert len(result.journal) == 1  # один реальный вызов


def test_external_journal_can_dedupe_across_calls() -> None:
    """Если передан внешний журнал с уже отмеченным ключом — повторно не выполняем.

    Сценарий: parallel hedge — два претендента пытаются выполнить тот же
    план; второй получает уже заполненный журнал и должен пропустить
    дубли.
    """
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=0,
        ),
    ))
    # Первый прогон
    first = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={"schedule_reminder": _ok_schedule},
    )
    assert len(first.journal) == 1
    # Второй прогон с тем же журналом — не должен дублировать
    second = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={"schedule_reminder": _ok_schedule},
        journal=first.journal,
    )
    assert len(second.journal) == 1  # ничего не добавилось


# ─── entity_id извлечение ────────────────────────────────────────────


def test_entity_id_extracted_from_args() -> None:
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="cancel_reminder",
            args={"reminder_id": "rem_xyz"},
            action_index=0,
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={"cancel_reminder": _ok_cancel},
    )
    entry = result.journal.entries[0]
    assert entry.entity_id == "rem_xyz"


def test_entity_id_extracted_from_result_when_create() -> None:
    """Для PER_ENTITY с entity_id в результате callable'а."""
    # complete_task требует task_id в required_fields (R-39 review fix)
    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="complete_task",
            args={"title": "Купить хлеб", "task_id": "task_123"},
            action_index=0,
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={"complete_task": lambda title, task_id: {"task_id": task_id}},
    )
    entry = result.journal.entries[0]
    assert entry.entity_id == "task_123"


# ─── result_data_extractor ─────────────────────────────────────────────


def test_custom_result_data_extractor() -> None:
    """Кастомный extractor может добавить trigger_human из date_formatter."""
    def extractor(tool_name: str, args: dict, raw: object) -> dict:
        out = dict(args)
        if tool_name == "schedule_reminder" and "trigger_iso" in args:
            out["trigger_human"] = "сегодня в 14:00"  # упрощённо для теста
        return out

    plan = ExecutionPlan(calls=(
        ToolCall(
            tool_name="schedule_reminder",
            args={"title": "X", "trigger_iso": "2026-05-17T14:00:00+03:00"},
            action_index=0,
        ),
    ))
    result = execute_plan(
        plan, tenant_id=42, turn_id="t-001",
        tool_callables={"schedule_reminder": _ok_schedule},
        result_data_extractor=extractor,
    )
    assert result.journal.entries[0].result_data["trigger_human"] == "сегодня в 14:00"
