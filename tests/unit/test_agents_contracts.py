"""R-39: тесты декларативных контрактов инструментов.

Главный смысл — стартап-инвариант: не дать запустить процесс с
некорректным реестром (мёртвые шаблоны, пропущенные плейсхолдеры,
несоответствие стратегии идемпотентности и полей).
"""

from __future__ import annotations

import pytest

from sreda.agents.contracts import (
    TOOL_CONTRACTS,
    ContractError,
    IdempotencyStrategy,
    MutationKind,
    ResultKind,
    ToolContract,
    ToolJournalEntry,
    TurnContext,
    assert_contracts_well_formed,
)


# ─── Реестр ───────────────────────────────────────────────────────────


def test_registry_contains_six_key_tools() -> None:
    """6 ключевых инструментов День 2."""
    expected = {
        "schedule_reminder",
        "cancel_reminder",
        "replace_reminder",
        "save_recipe",
        "add_shopping_items",
        "complete_task",
    }
    assert set(TOOL_CONTRACTS) >= expected


def test_all_registered_contracts_are_mutating() -> None:
    """Реестр первой версии содержит только пишущие — read-tools в день 3."""
    for name, contract in TOOL_CONTRACTS.items():
        assert contract.mutating, f"{name} не mutating, но в реестре"


# ─── Стартап-инвариант ────────────────────────────────────────────────


def test_startup_assert_passes_on_real_registry() -> None:
    """Главное — реальный TOOL_CONTRACTS проходит проверку."""
    assert_contracts_well_formed()


def test_startup_fails_when_success_variants_too_few() -> None:
    """Меньше 3 success-вариантов → ContractError."""
    bad = {
        "broken_tool": ToolContract(
            mutating=True,
            mutation_kind=MutationKind.WRITE,
            action_type="x",
            required_fields={"title": "string"},
            idempotency_strategy=IdempotencyStrategy.PER_TURN,
            success_template_variants=("«{title}»",),  # только 1
            failure_template_variants=("не получилось",),
        ),
    }
    with pytest.raises(ContractError, match="success_template_variants короче"):
        assert_contracts_well_formed(bad)


def test_startup_fails_when_failure_variants_empty() -> None:
    bad = {
        "broken_tool": ToolContract(
            mutating=True,
            mutation_kind=MutationKind.WRITE,
            action_type="x",
            required_fields={"title": "string"},
            idempotency_strategy=IdempotencyStrategy.PER_TURN,
            success_template_variants=("a {title}", "b {title}", "c {title}"),
            failure_template_variants=(),
        ),
    }
    with pytest.raises(ContractError, match="failure_template_variants пуст"):
        assert_contracts_well_formed(bad)


def test_startup_fails_when_supports_partial_but_no_variants() -> None:
    bad = {
        "x": ToolContract(
            mutating=True,
            mutation_kind=MutationKind.WRITE,
            action_type="x",
            required_fields={"title": "string"},
            idempotency_strategy=IdempotencyStrategy.PER_TURN,
            success_template_variants=("a {title}", "b {title}", "c {title}"),
            failure_template_variants=("err",),
            supports_partial=True,
            partial_template_variants=(),
        ),
    }
    with pytest.raises(ContractError, match="supports_partial=True но partial_template_variants пуст"):
        assert_contracts_well_formed(bad)


def test_startup_fails_when_dead_partial_templates() -> None:
    """supports_partial=False + непустой partial = мёртвые шаблоны."""
    bad = {
        "x": ToolContract(
            mutating=True,
            mutation_kind=MutationKind.WRITE,
            action_type="x",
            required_fields={"title": "string"},
            idempotency_strategy=IdempotencyStrategy.PER_TURN,
            success_template_variants=("a {title}", "b {title}", "c {title}"),
            failure_template_variants=("err",),
            supports_partial=False,
            partial_template_variants=("dead {title}",),
        ),
    }
    with pytest.raises(ContractError, match="мёртвые шаблоны"):
        assert_contracts_well_formed(bad)


def test_startup_fails_when_required_field_missing_from_success() -> None:
    """Если required_field ни в одном success-варианте — ошибка."""
    bad = {
        "x": ToolContract(
            mutating=True,
            mutation_kind=MutationKind.WRITE,
            action_type="x",
            required_fields={"title": "string", "extra_field": "string"},
            idempotency_strategy=IdempotencyStrategy.PER_TURN,
            success_template_variants=("a {title}", "b {title}", "c {title}"),
            failure_template_variants=("err",),
        ),
    }
    with pytest.raises(ContractError, match="плейсхолдер.*extra_field"):
        assert_contracts_well_formed(bad)


def test_startup_fails_natural_key_without_fields() -> None:
    bad = {
        "x": ToolContract(
            mutating=True,
            mutation_kind=MutationKind.WRITE,
            action_type="x",
            required_fields={"title": "string"},
            idempotency_strategy=IdempotencyStrategy.NATURAL_KEY,
            natural_key_fields=(),
            success_template_variants=("a {title}", "b {title}", "c {title}"),
            failure_template_variants=("err",),
        ),
    }
    with pytest.raises(ContractError, match="NATURAL_KEY требует natural_key_fields"):
        assert_contracts_well_formed(bad)


def test_startup_fails_entity_id_field_not_in_required() -> None:
    """R-39 review MAJOR 1: entity_id_field должно быть в required_fields."""
    bad = {
        "x": ToolContract(
            mutating=True,
            mutation_kind=MutationKind.DELETE,
            action_type="x",
            required_fields={"title": "string"},  # task_id отсутствует
            idempotency_strategy=IdempotencyStrategy.PER_ENTITY,
            entity_id_field="task_id",
            success_template_variants=("a {title}", "b {title}", "c {title}"),
            failure_template_variants=("err",),
        ),
    }
    with pytest.raises(ContractError, match="task_id.*required_fields"):
        assert_contracts_well_formed(bad)


def test_startup_fails_natural_key_field_not_in_required() -> None:
    """R-39 review MAJOR 1: natural_key_fields должны быть в required_fields."""
    bad = {
        "x": ToolContract(
            mutating=True,
            mutation_kind=MutationKind.WRITE,
            action_type="x",
            required_fields={"title": "string"},  # nonexistent_field отсутствует
            idempotency_strategy=IdempotencyStrategy.NATURAL_KEY,
            natural_key_fields=("title", "nonexistent_field"),
            success_template_variants=("a {title}", "b {title}", "c {title}"),
            failure_template_variants=("err",),
        ),
    }
    with pytest.raises(ContractError, match="nonexistent_field.*required_fields"):
        assert_contracts_well_formed(bad)


def test_startup_fails_per_entity_without_id_field() -> None:
    bad = {
        "x": ToolContract(
            mutating=True,
            mutation_kind=MutationKind.WRITE,
            action_type="x",
            required_fields={"title": "string"},
            idempotency_strategy=IdempotencyStrategy.PER_ENTITY,
            entity_id_field=None,
            success_template_variants=("a {title}", "b {title}", "c {title}"),
            failure_template_variants=("err",),
        ),
    }
    with pytest.raises(ContractError, match="PER_ENTITY требует entity_id_field"):
        assert_contracts_well_formed(bad)


# ─── Поведенческие свойства контрактов ────────────────────────────────


def test_schedule_reminder_uses_natural_key() -> None:
    c = TOOL_CONTRACTS["schedule_reminder"]
    assert c.idempotency_strategy is IdempotencyStrategy.NATURAL_KEY
    assert "title" in c.natural_key_fields
    assert "trigger_iso" in c.natural_key_fields


def test_replace_reminder_supports_partial() -> None:
    """Атомарный replace может частично сработать → partial templates."""
    c = TOOL_CONTRACTS["replace_reminder"]
    assert c.supports_partial is True
    assert c.partial_template_variants
    assert c.mutation_kind is MutationKind.REPLACE


def test_cancel_reminder_uses_per_entity() -> None:
    c = TOOL_CONTRACTS["cancel_reminder"]
    assert c.idempotency_strategy is IdempotencyStrategy.PER_ENTITY
    assert c.entity_id_field == "reminder_id"
    assert c.mutation_kind is MutationKind.DELETE


def test_each_contract_has_at_least_three_success_variants() -> None:
    """Достаточная вариативность чтобы случайный выбор не повторял ответы."""
    for name, contract in TOOL_CONTRACTS.items():
        assert len(contract.success_template_variants) >= 3, (
            f"{name}: только {len(contract.success_template_variants)} success-вариантов"
        )


# ─── ToolJournalEntry / TurnContext ────────────────────────────────────


def test_tool_journal_entry_minimum_fields() -> None:
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": "Разбудить Катю", "trigger_human": "сегодня в 14:00"},
    )
    assert entry.tool_name == "schedule_reminder"
    assert entry.result_kind is ResultKind.SUCCESS
    assert entry.error_message is None


def test_turn_context_defaults_to_moscow() -> None:
    ctx = TurnContext(turn_id="t1", tenant_id=42)
    assert ctx.user_tz == "Europe/Moscow"


def test_turn_context_is_frozen() -> None:
    ctx = TurnContext(turn_id="t1", tenant_id=42)
    with pytest.raises(Exception):  # FrozenInstanceError, но точное имя зависит от версии
        ctx.tenant_id = 99  # type: ignore[misc]
