# -*- coding: utf-8 -*-
"""#143 — валидатор должен статически проверять args-ссылки ${sN.field}
против выходной схемы продюсера sN (RED-before-impl).

Прецедент (#133, прод): план «отмени задачу про интернет» →
``cancel_task(task_id="${s1.items.only.task_id}")``, но у list_tasks поле
выдачи ``tasks``, не ``items`` → ``arg_violation`` НА ИСПОЛНЕНИИ → «поломка».
Должно ловиться НА ВАЛИДАЦИИ (мягкий ретрай с подсказкой), не крашем.

До фазы C эти тесты КРАСНЫЕ: args-ссылки проверяются только на существование
шага/self-ref (validator.py:_phase1_check_refs), но НЕ на существование поля.
compose-ссылки уже проверяются (_maybe_check_field_path) — переиспользуем для args.

Контракт (decision-log r2..r6):
- неверное поле продюсера в args-ссылке → нарушение НА ВАЛИДАЦИИ;
- `.only`-aware: ``${s1.tasks.only.task_id}`` валиден (tasks — список, элемент имеет
  task_id) и НЕ должен ложно отклоняться;
- branch-aware: поле, существующее в success-варианте (ListTasksOk), принимается,
  хотя в empty-варианте его нет.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field as PydField

from sreda.runtime.planner.schemas import (
    Action,
    ComposerCall,
    OutcomeBranch,
    Plan,
    TurnClassification,
)
from sreda.runtime.planner.validator import Violation, validate_plan
from sreda.services.tool_schemas.base import ToolOutput, ToolSpec


# --- фикстуры: продюсер с union-выдачей (список + терминальные ветки) --------
class _TaskRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    title: str


class _ListTasksOk(ToolOutput):
    status: Literal["ok"] = "ok"
    tasks: list[_TaskRow] = PydField(default_factory=list)


class _ListTasksEmpty(ToolOutput):
    status: Literal["empty"] = "empty"


class _ListTasksError(ToolOutput):
    status: Literal["error"] = "error"
    error_code: str = "x"


_ListTasksOutput = Annotated[
    Union[_ListTasksOk, _ListTasksEmpty, _ListTasksError],
    PydField(discriminator="status"),
]


class _ListTasksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_match: str | None = None


class _CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str


class _CancelOk(ToolOutput):
    status: Literal["ok"] = "ok"


def _spec(name, input_model, output_model, *, effect="read", domains=None) -> ToolSpec:
    return ToolSpec(  # type: ignore[arg-type]
        name=name,
        description=f"Test spec for {name}",
        family="tasks",
        effect=effect,
        read_domains=["tasks"] if effect == "read" else [],
        write_domains=["tasks"] if effect == "write" else [],
        input_model=input_model,
        output_model=output_model,
    )


def _registry() -> dict:
    return {
        "list_tasks": _spec("list_tasks", _ListTasksInput, _ListTasksOutput),
        "cancel_task": _spec("cancel_task", _CancelInput, _CancelOk, effect="write"),
    }


def _plan(cancel_args: dict) -> Plan:
    return Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="test"),
        actions={
            "s1": Action(
                tool="list_tasks", args={"title_match": "интернет"},
                expected_outcomes=[
                    OutcomeBranch(match={"status": "ok"}),
                    # .only требует терминальную ветку empty у продюсера (rule 2e)
                    OutcomeBranch(match={"status": "empty"}),
                ],
                depends_on=[],
            ),
            "s2": Action(
                tool="cancel_task", args=cancel_args,
                expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
                depends_on=["s1"],
            ),
        },
        compose=ComposerCall(kind="template", template_id="task_cancelled_ok"),
    )


_FIELD_CODES = {"arg_ref_unknown_field", "compose_ref_unknown_field"}


def test_wrong_producer_field_in_arg_ref_is_rejected() -> None:
    # ГЛАВНОЕ (#143): ${s1.items.only.task_id} — поля items у list_tasks НЕТ
    # (есть tasks) → нарушение НА ВАЛИДАЦИИ, а не arg_violation на исполнении.
    plan = _plan({"task_id": "${s1.items.only.task_id}"})
    violations = validate_plan(plan, _registry())
    field_v = [v for v in violations if v.code in _FIELD_CODES]
    assert field_v, (
        "ожидалось нарушение про несуществующее поле 'items' у продюсера s1 "
        f"(list_tasks → tasks); получено: {[(v.code, v.message) for v in violations]}"
    )
    assert any("items" in (v.message or "") for v in field_v)


def test_correct_field_with_only_selector_not_rejected() -> None:
    # СТРАХОВКА от ложного отклонения: ${s1.tasks.only.task_id} валиден —
    # tasks это список, .only берёт элемент, у элемента есть task_id.
    plan = _plan({"task_id": "${s1.tasks.only.task_id}"})
    violations = validate_plan(plan, _registry())
    field_v = [v for v in violations if v.code in _FIELD_CODES]
    assert not field_v, (
        "корректная ссылка ${s1.tasks.only.task_id} НЕ должна давать field-нарушение; "
        f"получено: {[(v.code, v.message) for v in field_v]}"
    )


def test_nonexistent_element_field_under_only_is_rejected() -> None:
    # ${s1.tasks.only.bogus} — поля bogus у элемента списка нет → нарушение.
    plan = _plan({"task_id": "${s1.tasks.only.bogus}"})
    violations = validate_plan(plan, _registry())
    assert any(v.code in _FIELD_CODES for v in violations), (
        f"ожидалось field-нарушение для .only.bogus; "
        f"получено: {[(v.code, v.message) for v in violations]}"
    )


# ---------------------------------------------------------------------------
# MAJOR-3 (Codex review #143 B): branch-aware сужение args-ссылок.
# Раньше args-ссылка валидировалась union-wide: поле из error-варианта
# продюсера проходило, даже если потребитель маршрутизирован ТОЛЬКО из
# status=ok → runtime interpolation failure («поломка»). Теперь, если у
# продюсера есть ветка с next==<consumer>, ссылка обязана быть валидна в
# КАЖДОМ таком достижимом статусе.
# ---------------------------------------------------------------------------
def _plan_next_routed(cancel_args: dict) -> Plan:
    """s1 МАРШРУТИЗИРУЕТ управление в s2 ТОЛЬКО из status=ok (next='s2'),
    empty — терминальная. s2 запускается лишь при ok-выходе s1."""
    return Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="test"),
        actions={
            "s1": Action(
                tool="list_tasks", args={"title_match": "интернет"},
                expected_outcomes=[
                    OutcomeBranch(match={"status": "ok"}, next="s2"),
                    OutcomeBranch(match={"status": "empty"}),
                ],
                depends_on=[],
            ),
            "s2": Action(
                tool="cancel_task", args=cancel_args,
                expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
                depends_on=["s1"],
            ),
        },
        compose=ComposerCall(kind="template", template_id="task_cancelled_ok"),
    )


def test_error_only_field_rejected_when_consumer_routed_from_ok() -> None:
    # ГЛАВНОЕ MAJOR-3: ${s1.error_code} есть ТОЛЬКО в error-варианте
    # продюсера, но s2 запускается лишь из status=ok (next='s2'). Branch-
    # aware валидатор обязан ОТКЛОНИТЬ — поле недостижимо в реальной ветке.
    plan = _plan_next_routed({"task_id": "${s1.error_code}"})
    violations = validate_plan(plan, _registry())
    field_v = [v for v in violations if v.code in _FIELD_CODES]
    assert field_v, (
        "ожидалось field-нарушение: error_code недостижим из status=ok; "
        f"получено: {[(v.code, v.message) for v in violations]}"
    )
    assert any("error_code" in (v.message or "") for v in field_v)


def test_ok_field_accepted_when_consumer_routed_from_ok() -> None:
    # СТРАХОВКА: ${s1.tasks.only.task_id} достижимо в status=ok (откуда и
    # маршрутизируется s2) → НЕ должно отклоняться при сужении.
    plan = _plan_next_routed({"task_id": "${s1.tasks.only.task_id}"})
    violations = validate_plan(plan, _registry())
    field_v = [v for v in violations if v.code in _FIELD_CODES]
    assert not field_v, (
        "корректная ссылка из достижимого ok-статуса не должна давать "
        f"field-нарушение; получено: {[(v.code, v.message) for v in field_v]}"
    )


# ---------------------------------------------------------------------------
# Критерий приёмки #3 (high R6): отрицательная гарантия legacy title-пути
# чек-листов. mark/delete теперь принимают ТОЛЬКО item_id; план с legacy
# item_title_match / без item_id отклоняется НА ВАЛИДАЦИИ (на реальном реестре).
# ---------------------------------------------------------------------------
def _real_registry() -> dict:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    return {s.name: s for s in MIGRATED_TOOL_SPECS}


def _checklist_destructive_plan(mark_args: dict) -> Plan:
    return Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="test"),
        actions={
            "s1": Action(
                tool="mark_checklist_item_done", args=mark_args,
                expected_outcomes=[OutcomeBranch(match={"status": "done"})],
                depends_on=[],
            ),
        },
        compose=ComposerCall(kind="template", template_id="task_cancelled_ok"),
    )


def test_legacy_title_path_checklist_rejected() -> None:
    # high R6: mark_checklist_item_done с legacy item_title_match (без item_id)
    # → НЕвалиден (item_title_match нет в planner-схеме, item_id обязателен).
    plan = _checklist_destructive_plan(
        {"list_id_or_title": "дача", "item_title_match": "лопата"}
    )
    violations = validate_plan(plan, _real_registry())
    assert violations, "legacy title-путь чек-листа должен отклоняться валидатором"
    codes = {v.code for v in violations}
    assert codes & {"unknown_arg", "invalid_arg_type", "missing_required_arg",
                    "schema_violation"}, (
        f"ожидалось отклонение legacy-аргументов / отсутствия item_id; коды: {codes}"
    )


def test_checklist_mark_with_item_id_is_accepted() -> None:
    # Канонический id-путь не должен давать нарушений args/схемы.
    plan = _checklist_destructive_plan({"item_id": "clitem_" + "0" * 24})
    violations = validate_plan(plan, _real_registry())
    arg_v = [v for v in violations
             if v.code in {"unknown_arg", "invalid_arg_type", "missing_required_arg",
                           "arg_ref_unknown_field"}]
    assert not arg_v, f"корректный id-путь не должен давать arg/field-нарушений: {arg_v}"
