"""#124-1 — регрессия хода 1 провальной прод-сессии 2026-06-10.

Планировщик положил пункты чек-листа объектами ``{"title": "Обувь"}``
вместо строк → плановый валидатор дал 10 нарушений «Input should be a
valid string» → invalid_plan_after_retry → пользователю «Не получилось
разобрать». Коэрция живёт в аннотации ``ChecklistItemTitle``
(BeforeValidator), поэтому работает на ВСЕХ путях валидации:

- плановая, args без рефов → полный ``model_validate``
  (``test_plan_validator_accepts_title_object_items``);
- плановая, args С рефами → per-field ``TypeAdapter(annotation)`` —
  ИМЕННО ради этого пути коэрция в аннотации, а не в модельном
  ``@field_validator`` (``test_plan_validator_refs_path_coerces_items``;
  субагент R1 MAJOR: без этого пина ужесточение refs-пути тихо вернёт
  инцидент-класс при зелёных тестах);
- исполнение (``validate_args_at_execute_time`` → model_validate).

Чек-лист принятия #124-1 называет именно эти тесты.
"""
from __future__ import annotations

from sreda.runtime.planner.schemas import (
    Action,
    ComposerCall,
    OutcomeBranch,
    Plan,
    TurnClassification,
)
from sreda.runtime.planner.validator import validate_plan
from sreda.services.tool_schemas.specs_checklists import CHECKLISTS_SPECS

_REGISTRY = {s.name: s for s in CHECKLISTS_SPECS}

# Аргументы попытки 1 из прод-сессии (форма; названия — синтетика #124)
_TITLE_OBJECT_ARGS = {
    "list_id_or_title": "Вещи в поездку",
    "items": [
        {"title": "Кроссовки"},
        {"title": "Носки 4-5 пар"},
        {"title": "Футболки 3 штуки"},
    ],
}


def _plan_add_items(args: dict) -> Plan:
    return Plan(
        turn_classification=TurnClassification(
            is_new_turn=True, reason="вещи в поездку"
        ),
        actions={
            "s1": Action(
                tool="add_checklist_items",
                args=args,
                expected_outcomes=[OutcomeBranch(match={"status": "added"})],
                depends_on=[],
            ),
        },
        compose=ComposerCall(
            kind="template",
            template_id="checklist_show",
            template_data={
                "title": "Вещи в поездку",
                "items": ["Кроссовки", "Носки 4-5 пар", "Футболки 3 штуки"],
            },
        ),
    )


def test_plan_validator_accepts_title_object_items() -> None:
    """Ход 1: план с items-объектами теперь проходит плановую валидацию."""
    plan = _plan_add_items(_TITLE_OBJECT_ARGS)
    violations = validate_plan(plan, _REGISTRY)
    assert violations == [], [f"{v.code}:{v.field_path}" for v in violations]


def test_plan_validator_refs_path_coerces_items() -> None:
    """Args С рефом → validator идёт per-field TypeAdapter-путём;
    литеральные items-объекты обязаны коэрцироваться и там."""
    plan = Plan(
        turn_classification=TurnClassification(
            is_new_turn=True, reason="вещи в поездку"
        ),
        actions={
            "s1": Action(
                tool="create_checklist",
                args={"title": "Вещи в поездку"},
                expected_outcomes=[
                    OutcomeBranch(match={"status": "created"})
                ],
                depends_on=[],
            ),
            "s2": Action(
                tool="add_checklist_items",
                args={
                    "list_id_or_title": "${s1.checklist_id}",
                    "items": [{"title": "Кроссовки"}, {"title": "Носки"}],
                },
                expected_outcomes=[
                    OutcomeBranch(match={"status": "added"})
                ],
                depends_on=["s1"],
            ),
        },
        compose=ComposerCall(
            kind="template",
            template_id="checklist_show",
            template_data={
                "title": "Вещи в поездку",
                "items": ["Кроссовки", "Носки"],
            },
        ),
    )
    violations = validate_plan(plan, _REGISTRY)
    assert violations == [], [f"{v.code}:{v.field_path}" for v in violations]


def test_plan_validator_still_rejects_object_with_extra_keys() -> None:
    """{"title": …, "count": 5} НЕ коэрцируем (потеря count) — нарушение
    остаётся, ретрай-фидбек учит модель строкам."""
    plan = _plan_add_items({
        "list_id_or_title": "Вещи в поездку",
        "items": [{"title": "Носки", "count": 5}],
    })
    violations = validate_plan(plan, _REGISTRY)
    assert violations, "объект с лишними ключами обязан давать нарушение"


def test_plan_with_created_echo_binding_passes() -> None:
    """Пин канонической формы из рецепта (Codex R2 MINOR): эхо добавленного
    через ${s1.created} в checklist_show — ровно та форма, что дала 6/6
    валидных планов с первой попытки в живой серии."""
    plan = Plan(
        turn_classification=TurnClassification(
            is_new_turn=True, reason="вещи в поездку"
        ),
        actions={
            "s1": Action(
                tool="add_checklist_items",
                args={
                    "list_id_or_title": "Вещи в поездку",
                    "items": ["Кроссовки", "Носки 4-5 пар"],
                },
                expected_outcomes=[OutcomeBranch(match={"status": "added"})],
                depends_on=[],
            ),
        },
        compose=ComposerCall(
            kind="template",
            template_id="checklist_show",
            template_data={
                "title": "Вещи в поездку",
                "items": "${s1.created}",
            },
        ),
    )
    violations = validate_plan(plan, _REGISTRY)
    assert violations == [], [f"{v.code}:{v.field_path}" for v in violations]


def test_checklist_show_duplicate_only_renders_bare_title() -> None:
    """Документирующий пин (Codex R2 MAJOR-контекст): created=[] →
    checklist_show рендерит ТОЛЬКО заголовок — потому рецепт ограничен
    веткой status=added; для added_with_dups привязка НЕ рекомендуется."""
    from sreda.services.composer.registry import render

    out = render("checklist_show", {"title": "Вещи в поездку", "items": []})
    assert out == "Вещи в поездку:"


def test_execute_time_validation_returns_coerced_strings() -> None:
    """Исполнитель получает уже строки — рантайм-инструмент не видит dict'ов."""
    spec = _REGISTRY["add_checklist_items"]
    validated = spec.validate_args_at_execute_time(_TITLE_OBJECT_ARGS)
    assert validated["items"] == [
        "Кроссовки", "Носки 4-5 пар", "Футболки 3 штуки",
    ]
