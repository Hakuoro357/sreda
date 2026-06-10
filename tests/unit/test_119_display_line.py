"""#119 — чистое поле display_line у списковых моделей (red-before-impl).

Находка приёмочного регресса #118: «В списке покупок: • [sh_8794…] молоко» —
`raw_line` хранит строку целиком с [id]-префиксом (задумано для планировщика),
а шаблоны рендерили его пользователю. Фикс «две полочки» (владелец, 2026-06-10):
`display_line` без номера — для показа; `raw_line` остаётся для ссылок мозга.

Чеклист #119:
- display_line без [..]-префикса у всех трёх списковых моделей;
- рендер show-шаблонов на типизированной выдаче не содержит внутренних номеров;
- контракт литеральных элементов требует display_line (не raw_line).
"""

from __future__ import annotations

import pytest

from sreda.services.composer.registry import REGISTRY as _COMPOSER_REGISTRY
from sreda.services.tool_schemas.housewife import (
    ListRemindersList,
    ListShoppingItems,
    SearchRecipesList,
    parse_list_reminders,
    parse_list_shopping,
    parse_search_recipes,
)

SH_A = "sh_" + "a" * 24
SH_B = "sh_" + "b" * 24
REM_A = "rem_" + "1" * 24
REC_A = "rec_" + "2" * 24

_SHOPPING_WIRE = (
    "pending shopping items:\n"
    "[молочные]\n"
    f"  [{SH_A}] молоко (1 л)\n"
    f"  [{SH_B}] хлеб"
)

_REMINDERS_WIRE = (
    "active reminders:\n"
    f"[{REM_A}] купить хлеб → 2026-05-26 18:00"
)


def test_shopping_items_have_clean_display_line():
    result = parse_list_shopping(_SHOPPING_WIRE)
    assert isinstance(result, ListShoppingItems)
    assert result.items[0].display_line == "молоко (1 л)"
    assert result.items[1].display_line == "хлеб"
    # сырая строка остаётся как была — для планировщика/ссылок
    assert SH_A in result.items[0].raw_line


def test_reminders_items_have_clean_display_line():
    result = parse_list_reminders(_REMINDERS_WIRE)
    assert isinstance(result, ListRemindersList)
    assert result.items[0].display_line == "купить хлеб → 2026-05-26 18:00"
    assert REM_A in result.items[0].raw_line


def test_search_recipes_items_have_clean_display_line():
    raw = f"1 recipe(s):\n  [{REC_A}] Борщ · классика · 90 мин"
    result = parse_search_recipes(raw)
    assert isinstance(result, SearchRecipesList)
    assert result.items[0].display_line == "Борщ · классика · 90 мин"
    assert REC_A in result.items[0].raw_line


def test_shopping_show_render_has_no_internal_ids():
    """Ровно сценарий утечки: рендер show-шаблона на ТИПИЗИРОВАННОЙ выдаче
    (как её разворачивает ссылка ${s1.items}) не содержит номеров."""
    parsed = parse_list_shopping(_SHOPPING_WIRE)
    items = [it.model_dump() for it in parsed.items]
    out = _COMPOSER_REGISTRY.render("shopping_list_show", {"items": items})
    assert "молоко (1 л)" in out and "хлеб" in out
    assert "sh_" not in out, f"внутренний номер утёк в ответ: {out!r}"


def test_reminders_show_render_has_no_internal_ids():
    parsed = parse_list_reminders(_REMINDERS_WIRE)
    items = [it.model_dump() for it in parsed.items]
    out = _COMPOSER_REGISTRY.render("reminders_list_show", {"items": items})
    assert "купить хлеб" in out
    assert "rem_" not in out, f"внутренний номер утёк в ответ: {out!r}"


def test_item_contract_requires_display_line():
    # литеральный элемент-объект обязан нести именно display_line
    from sreda.services.composer_contracts import get_composer_contract

    contract = get_composer_contract("shopping_list_show")
    errors = contract(
        {"items": [{"raw_line": f"[{SH_A}] молоко"}]}, allow_refs=True,
    )
    assert errors and any("display_line" in e for e in errors)
    assert contract({"items": [{"display_line": "молоко"}]}, allow_refs=True) == []


# --- лазейка ссылок на raw_line (Codex #119 R1 MAJOR) -------------------------


def _plan_items_ref(template_id: str, items_value) -> "Plan":
    from sreda.runtime.planner.schemas import Plan

    return Plan.model_validate({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "list_shopping",
                "args": {},
                "expected_outcomes": [
                    {"match": {"status": "ok"}, "next": None},
                    {"match": {"status": "empty"}, "next": None},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": template_id,
            "template_data": {"items": items_value},
        },
    })


def _contract_codes(plan) -> list[str]:
    from sreda.runtime.planner.validator import validate_plan
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

    reg = {s.name: s for s in MIGRATED_TOOL_SPECS}
    return [
        v.message for v in validate_plan(
            plan, reg,
            composer_template_ids=frozenset(_COMPOSER_REGISTRY.template_ids()),
        )
        if v.code == "composer_contract_invalid"
    ]


def test_raw_line_element_ref_rejected():
    # ссылка-элемент на raw_line после исполнения — строка с [id]-префиксом,
    # строковая ветка отрендерит её как есть → утечка уровнем глубже
    plan = _plan_items_ref(
        "shopping_list_show", ["${s1.items.only.raw_line}"],
    )
    msgs = _contract_codes(plan)
    assert msgs and any("raw_line" in m and "display_line" in m for m in msgs)


def test_raw_line_whole_value_ref_rejected():
    plan = _plan_items_ref("shopping_list_show", "${s1.items.only.raw_line}")
    msgs = _contract_codes(plan)
    assert msgs and any("raw_line" in m for m in msgs)


def test_raw_line_field_value_ref_rejected():
    plan = _plan_items_ref(
        "shopping_list_show",
        [{"display_line": "${s1.items.only.raw_line}"}],
    )
    msgs = _contract_codes(plan)
    assert msgs and any("raw_line" in m for m in msgs)


def test_display_line_refs_accepted():
    plan = _plan_items_ref(
        "shopping_list_show", [{"display_line": "${s1.items.only.display_line}"}],
    )
    assert _contract_codes(plan) == []
    plan2 = _plan_items_ref("shopping_list_show", "${s1.items}")
    assert _contract_codes(plan2) == []


# --- сквозной круг через ToolSpec.process_output (Codex #119 R1 MINOR) --------


def test_process_output_round_trip_non_empty_wires():
    """Парсер ↔ output_model не разъедутся молча: process_output на НЕпустых
    легаси-строках всех трёх инструментов отдаёт и raw_line, и display_line."""
    from sreda.services.tool_schemas.specs_recipes import SEARCH_RECIPES_SPEC
    from sreda.services.tool_schemas.specs_reminders import LIST_REMINDERS_SPEC
    from sreda.services.tool_schemas.specs_shopping import LIST_SHOPPING_SPEC

    shop = LIST_SHOPPING_SPEC.process_output(_SHOPPING_WIRE)
    assert shop.items[0].display_line == "молоко (1 л)"
    assert SH_A in shop.items[0].raw_line

    rem = LIST_REMINDERS_SPEC.process_output(_REMINDERS_WIRE)
    assert rem.items[0].display_line == "купить хлеб → 2026-05-26 18:00"
    assert REM_A in rem.items[0].raw_line

    rec = SEARCH_RECIPES_SPEC.process_output(
        f"1 recipe(s):\n  [{REC_A}] Борщ · классика"
    )
    assert rec.items[0].display_line == "Борщ · классика"
    assert REC_A in rec.items[0].raw_line
