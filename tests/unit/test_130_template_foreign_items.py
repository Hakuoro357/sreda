"""P1 2026-06-11 (#130-сосед): чужая форма элементов не должна валить рендер.

Прод-инцидент: «покажи дела» → планировщик исполнил чек-листовые шаги, но
собрал их reminders_list_show; у элементов чек-листа нет display_line →
StrictUndefined уронил рендер → compose_error → пользователю страховка.
Валидатор это пропускает (ссылка ${sN.items} существует; внутрь словарей
по ссылке не заглядывает — слепое пятно #118).

Контракт после фикса: show-шаблоны списков переживают словари ЧУЖОЙ формы
(деградация до доступного поля / маркера), а не взрываются. Правильный
шаблон — забота рецептов в описаниях инструментов; этот слой — страховка.
"""
from __future__ import annotations

from sreda.services.composer.registry import render

_CHECKLIST_SHAPED = [
    {"title": "колодки", "item_status": "pending"},
    {"title": "масло", "item_status": "done"},
]
_REMINDER_SHAPED = [
    {"reminder_id": "rem_" + "0" * 24, "raw_line": "[rem_x] хлеб",
     "display_line": "завтра 09:00 — хлеб"},
]


def test_reminders_show_survives_checklist_shaped_items() -> None:
    """Ровно прод-падение: чек-листовые словари в reminders_list_show."""
    out = render("reminders_list_show", {"items": _CHECKLIST_SHAPED})
    assert "колодки" in out and "масло" in out


def test_shopping_show_survives_checklist_shaped_items() -> None:
    out = render("shopping_list_show", {"items": _CHECKLIST_SHAPED})
    assert "колодки" in out


def test_checklist_show_survives_reminder_shaped_items() -> None:
    out = render("checklist_show", {"title": "Дела", "items": _REMINDER_SHAPED})
    assert "хлеб" in out


def test_native_shapes_unchanged() -> None:
    """Родные формы рендерятся как раньше (регрессия на штатный путь)."""
    out = render("reminders_list_show", {"items": _REMINDER_SHAPED})
    assert "завтра 09:00 — хлеб" in out
    out2 = render("checklist_show", {
        "title": "Дела",
        "items": [{"title": "масло", "item_status": "done"}],
    })
    assert "масло ✅" in out2


def test_none_and_empty_values_degrade_not_leak() -> None:
    """Субагент R1 MAJOR: None/'' — определённые значения, default без
    булевой формы их пропускал → пользователю утекало «• None»."""
    out = render("reminders_list_show", {"items": [
        {"display_line": None, "title": "хлеб"},
        {"display_line": ""},
        {},
    ]})
    assert "None" not in out
    assert "хлеб" in out          # деградация к title работает
    assert "(пункт)" in out       # полный провал формы — маркер


def test_plan_validator_rejects_foreign_show_template_source() -> None:
    """Codex R1 medium MAJOR: пара «источник items → show-шаблон»
    проверяется на ПЛАНЕ (ретрай-фидбек), а не только телеметрией."""
    from sreda.runtime.planner.schemas import (
        Action, ComposerCall, OutcomeBranch, Plan, TurnClassification,
    )
    from sreda.runtime.planner.validator import validate_plan
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

    registry = {s.name: s for s in MIGRATED_TOOL_SPECS}

    def _plan(template_id: str, tool: str, status: str) -> Plan:
        return Plan(
            turn_classification=TurnClassification(
                is_new_turn=True, reason="показ"
            ),
            actions={
                "s1": Action(
                    tool=tool, args={"list_id_or_title": "Дела"}
                    if tool == "show_checklist" else {},
                    expected_outcomes=[OutcomeBranch(match={"status": status})],
                    depends_on=[],
                ),
            },
            compose=ComposerCall(
                kind="template", template_id=template_id,
                template_data={"title": "Дела", "items": "${s1.items}"},
            ),
        )

    # прод-кейс: чек-листовые items в шаблоне напоминаний → нарушение
    bad = _plan("reminders_list_show", "show_checklist", "ok")
    codes = {v.code for v in validate_plan(bad, registry)}
    assert "show_template_source_mismatch" in codes

    # родная пара → чисто
    good = _plan("checklist_show", "show_checklist", "ok")
    assert not [v for v in validate_plan(good, registry)
                if v.code == "show_template_source_mismatch"]

    # Codex R2 high MAJOR: родной инструмент, но НЕ поле-носитель списка
    # (items="${s1.title}" — строка итерировалась бы посимвольно)
    field_bad = Plan(
        turn_classification=TurnClassification(
            is_new_turn=True, reason="показ"
        ),
        actions={
            "s1": Action(
                tool="show_checklist",
                args={"list_id_or_title": "Дела"},
                expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
                depends_on=[],
            ),
        },
        compose=ComposerCall(
            kind="template", template_id="checklist_show",
            template_data={"title": "Дела", "items": "${s1.title}"},
        ),
    )
    codes2 = {v.code for v in validate_plan(field_bad, registry)}
    assert "show_template_source_mismatch" in codes2

    # эхо добавленного — родная пара add_checklist_items.created
    created_ok = Plan(
        turn_classification=TurnClassification(
            is_new_turn=True, reason="добавление"
        ),
        actions={
            "s1": Action(
                tool="add_checklist_items",
                args={"list_id_or_title": "Дела", "items": ["x"]},
                expected_outcomes=[OutcomeBranch(match={"status": "added"})],
                depends_on=[],
            ),
        },
        compose=ComposerCall(
            kind="template", template_id="checklist_show",
            template_data={"title": "Дела", "items": "${s1.created}"},
        ),
    )
    assert not [v for v in validate_plan(created_ok, registry)
                if v.code == "show_template_source_mismatch"]


def test_degraded_render_is_observable() -> None:
    """Субагент/оба Codex R1: деградация не должна быть невидимой —
    счётчик + warning при чужой форме."""
    from sreda.services.composer.compose import (
        TEMPLATE_SHAPE_DEGRADED_METRICS, _note_foreign_item_shapes,
    )

    before = TEMPLATE_SHAPE_DEGRADED_METRICS.get("reminders_list_show", 0)
    _note_foreign_item_shapes(
        "reminders_list_show",
        {"items": [{"title": "колодки", "item_status": "pending"}]},
    )
    assert TEMPLATE_SHAPE_DEGRADED_METRICS["reminders_list_show"] == before + 1
    # родная форма счётчик не трогает
    _note_foreign_item_shapes(
        "reminders_list_show", {"items": [{"display_line": "x"}]},
    )
    assert TEMPLATE_SHAPE_DEGRADED_METRICS["reminders_list_show"] == before + 1
