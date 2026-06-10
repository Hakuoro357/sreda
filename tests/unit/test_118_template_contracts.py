"""#118 — обязательные переменные шаблонов сборщика (red-before-impl).

Прогон r3 стенда #115 (199 реальных ходов): 6 ходов ушли в заглушку
``partial_with_compose_error`` потому что планировщик выбрал шаблон, но не
привязал его переменные — «'items' is undefined» всплывал только при рендере
(StrictUndefined). Валидатор УЖЕ умеет диспатчить контракты шаблонов
(``get_composer_contract``) и прокидывать нарушения в повтор планировщика —
но шаблоны с данными стояли как ``NO_CONTRACT``.

Чеклист #118:
- валидатор отклоняет план с шаблоном без обязательных переменных
  (``composer_contract_invalid`` называет недостающие ключи);
- ``${sN.field}``-ссылка считается присутствующим значением (allow_refs);
- каждый шаблон реестра рендерится на своём образце данных без исключений;
- анти-дрейф: НЕопциональные переменные Jinja-шаблона ⊆ требуемых контрактом.
"""

from __future__ import annotations

import pytest
from jinja2 import Environment, StrictUndefined, meta

from sreda.runtime.planner.validator import validate_plan
from sreda.runtime.planner.schemas import Plan
from sreda.services.composer.registry import REGISTRY as _COMPOSER_REGISTRY
from sreda.services.composer.templates_housewife import HOUSEWIFE_TEMPLATES
from sreda.services.composer_contracts import (
    NO_CONTRACT,
    RUNTIME_ONLY_TEMPLATE_KEYS,
    SAMPLE_TEMPLATE_DATA,
    TEMPLATE_OPTIONAL_KEYS,
    TEMPLATE_REQUIRED_KEYS,
    get_composer_contract,
)
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

_REAL_REGISTRY = {s.name: s for s in MIGRATED_TOOL_SPECS}
_REAL_TEMPLATE_IDS = frozenset(_COMPOSER_REGISTRY.template_ids())


def _plan_with_compose(template_id: str, template_data: dict) -> Plan:
    return Plan.model_validate({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                "args": {"items": [{"title": "молоко"}]},
                "expected_outcomes": [
                    {"match": {"status": "added"}, "next": None},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": template_id,
            "template_data": template_data,
        },
    })


def _contract_violations(plan: Plan) -> list:
    return [
        v for v in validate_plan(
            plan, _REAL_REGISTRY, composer_template_ids=_REAL_TEMPLATE_IDS,
        )
        if v.code == "composer_contract_invalid"
    ]


# --- валидатор: обязательные ключи ------------------------------------------


def test_shopping_added_ok_without_items_rejected():
    # ровно сценарий заглушки из прогона r3: шаблон выбран, данные не привязаны
    plan = _plan_with_compose("shopping_added_ok", {})
    violations = _contract_violations(plan)
    assert violations, "пустой template_data у shopping_added_ok обязан отклоняться"
    assert any("items" in v.message for v in violations)


def test_shopping_added_ok_with_ref_accepted():
    plan = _plan_with_compose("shopping_added_ok", {"items": "${s1.created}"})
    assert _contract_violations(plan) == []


def test_shopping_added_ok_with_literal_accepted():
    plan = _plan_with_compose("shopping_added_ok", {"items": ["молоко"]})
    assert _contract_violations(plan) == []


def test_shopping_added_ok_blank_value_rejected():
    plan = _plan_with_compose("shopping_added_ok", {"items": []})
    assert _contract_violations(plan), "пустой список не даёт осмысленного ответа"


@pytest.mark.parametrize(
    "template_id, missing_key",
    [
        ("shopping_list_show", "items"),
        ("checklist_show", "title"),
        ("checklist_empty", "title"),
        ("reminder_set_ok", "what"),
        ("recipe_show", "recipe_text"),
        ("recipe_not_found_ask_alt", "query"),
        ("ask_when_to_remind", "what"),
    ],
)
def test_data_templates_require_their_keys(template_id: str, missing_key: str):
    plan = _plan_with_compose(template_id, {})
    violations = _contract_violations(plan)
    assert violations, f"{template_id}: пустой template_data обязан отклоняться"
    assert any(missing_key in v.message for v in violations)


# --- вложенная форма элементов списков (Codex #118 R1 MAJOR) -----------------


def test_list_template_str_items_rejected():
    # шаблон рендерит it.raw_line — строковый элемент гарантированно роняет рендер
    plan = _plan_with_compose(
        "shopping_list_show", {"count": 1, "items": ["молоко"]},
    )
    violations = _contract_violations(plan)
    assert violations and any("raw_line" in v.message for v in violations)


def test_checklist_item_missing_status_rejected():
    # checklist_show рендерит it.item_status без сторожа — проверено эмпирически
    plan = _plan_with_compose(
        "checklist_show",
        {"title": "Дача", "items": [{"title": "грабли"}]},
    )
    violations = _contract_violations(plan)
    assert violations and any("item_status" in v.message for v in violations)


def test_list_template_ref_items_accepted():
    plan = _plan_with_compose(
        "shopping_list_show", {"count": "${s1.count}", "items": "${s1.items}"},
    )
    assert _contract_violations(plan) == []


def test_list_template_item_field_ref_accepted():
    plan = _plan_with_compose(
        "checklist_show",
        {"title": "Дача",
         "items": [{"title": "${s1.title}", "item_status": "${s1.status}"}]},
    )
    assert _contract_violations(plan) == []


def test_item_field_empty_container_rejected():
    # Codex R2 medium MINOR: пустые []/{} во вложенном поле — тоже «пусто»
    plan = _plan_with_compose(
        "shopping_list_show", {"count": 1, "items": [{"raw_line": []}]},
    )
    assert _contract_violations(plan), "raw_line=[] обязан отклоняться"
    plan2 = _plan_with_compose(
        "checklist_show",
        {"title": "Дача", "items": [{"title": {}, "item_status": "pending"}]},
    )
    assert _contract_violations(plan2), "title={} обязан отклоняться"


def test_item_fields_map_not_stale():
    """Codex R2 medium MINOR: вложенный контракт не должен пережить шаблон.

    Для каждого поля из _TEMPLATE_ITEM_FIELDS: рендер образца БЕЗ этого поля
    обязан падать. Если рендер прошёл — шаблон больше не требует поле, и
    контракт устарел (режет валидные планы)."""
    import copy

    from sreda.services.composer_contracts import _TEMPLATE_ITEM_FIELDS

    for tid, by_key in _TEMPLATE_ITEM_FIELDS.items():
        for list_key, fields in by_key.items():
            for f in fields:
                data = copy.deepcopy(SAMPLE_TEMPLATE_DATA[tid])
                assert data[list_key] and isinstance(data[list_key][0], dict)
                data[list_key][0].pop(f, None)
                with pytest.raises(Exception, match=f):
                    _COMPOSER_REGISTRY.render(tid, data)


def test_list_template_good_literal_items_accepted():
    plan = _plan_with_compose(
        "shopping_list_show",
        {"count": 1, "items": [{"raw_line": "молоко"}]},
    )
    assert _contract_violations(plan) == []


def test_extra_keys_are_allowed():
    # контракт проверяет ПОЛНОТУ, а не закрытый перечень: лишний ключ
    # безвреден (StrictUndefined его игнорирует) и не должен валить план
    plan = _plan_with_compose(
        "shopping_added_ok", {"items": ["молоко"], "note": "extra"},
    )
    assert _contract_violations(plan) == []


# --- реестр образцов: каждый шаблон рендерится -------------------------------


def test_every_template_has_sample_and_renders():
    """Каждый шаблон реестра рендерится на своём образце БЕЗ исключений.

    Ловит сразу два дрейфа: шаблон поменяли (новая переменная) — образец
    устарел → StrictUndefined здесь, а не заглушка в проде; образец врёт
    контракту — проверка контрактом ниже."""
    for tid in HOUSEWIFE_TEMPLATES:
        assert tid in SAMPLE_TEMPLATE_DATA, (
            f"шаблон {tid!r} без образца SAMPLE_TEMPLATE_DATA — добавь образец"
        )
        text = _COMPOSER_REGISTRY.render(tid, dict(SAMPLE_TEMPLATE_DATA[tid]))
        assert isinstance(text, str) and text.strip(), f"{tid}: пустой рендер"


def test_samples_satisfy_their_contracts():
    for tid in HOUSEWIFE_TEMPLATES:
        contract = get_composer_contract(tid)
        if contract is None or contract is NO_CONTRACT:
            continue
        errors = contract(dict(SAMPLE_TEMPLATE_DATA[tid]), allow_refs=False)
        assert errors == [], f"{tid}: образец не проходит собственный контракт: {errors}"


# --- анти-дрейф: переменные Jinja ⊆ контракт ---------------------------------


def test_required_keys_cover_all_non_optional_template_vars():
    """НЕопциональная переменная шаблона обязана быть в required-ключах.

    «Опциональная» = заявлена в TEMPLATE_OPTIONAL_KEYS (шаблон сторожит её
    через ``is defined``). Если шаблону добавят переменную и забудут контракт —
    этот тест упадёт ДО того, как заглушка дойдёт до пользователя."""
    env = Environment(undefined=StrictUndefined)
    # интроспекция компилирует шаблон — фильтр должен существовать
    env.filters["clarify_ru"] = lambda x: x
    for tid, source in HOUSEWIFE_TEMPLATES.items():
        declared = meta.find_undeclared_variables(env.parse(source))
        required = set(TEMPLATE_REQUIRED_KEYS.get(tid, ()))
        optional = set(TEMPLATE_OPTIONAL_KEYS.get(tid, ()))
        runtime_only = set(RUNTIME_ONLY_TEMPLATE_KEYS.get(tid, ()))
        uncovered = declared - required - optional - runtime_only
        assert not uncovered, (
            f"{tid}: переменные {sorted(uncovered)} не покрыты ни "
            f"TEMPLATE_REQUIRED_KEYS, ни TEMPLATE_OPTIONAL_KEYS, ни "
            f"RUNTIME_ONLY_TEMPLATE_KEYS"
        )
        # обратная проверка (Codex #118 R1 MINOR): требуемый ключ обязан
        # реально использоваться шаблоном — иначе контракт отклоняет валидные
        # планы ради переменной, которой больше нет
        stale = required - declared
        assert not stale, (
            f"{tid}: TEMPLATE_REQUIRED_KEYS требует {sorted(stale)}, "
            f"но шаблон эти переменные не рендерит — устаревший контракт"
        )


def test_contracted_templates_match_required_keys_map():
    # карта требований и реестр контрактов согласованы: у каждого шаблона из
    # карты — настоящий контракт, не NO_CONTRACT
    for tid in TEMPLATE_REQUIRED_KEYS:
        contract = get_composer_contract(tid)
        assert contract is not None and contract is not NO_CONTRACT, (
            f"{tid}: в TEMPLATE_REQUIRED_KEYS, но без контракта"
        )
