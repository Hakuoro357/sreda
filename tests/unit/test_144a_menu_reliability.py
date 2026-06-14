"""#144-A — надёжность ПОКАЗА и ПРАВКИ меню (срез #124).

Покрывает наблюдаемые критерии приёмки плана
``plans/144-menu-reliability-final.md`` для машинно-проверяемого ядра:

1. Канонический edit-план показа→правки проходит ``validate_plan``,
   когда правка ссылается на ПРАВИЛЬНОЕ поле выдачи list_menu
   (``${s1.menu_id}``). Неверное поле (``${s1.plan_id}``) — отклоняется
   валидатором (валидатор C #143: ``arg_ref_unknown_field``). Это
   закрепляет рассогласование ``menu_id`` / ``plan_id`` (риск из плана).

2. Презентер пустого меню рендерит канонический текст
   ``ListMenuEmpty.display_summary`` («…не составлено. Составить?») —
   без дрейфа в отдельный шаблон, без id, и НЕ из пула «поломок».

Гайд-строки в карточках (``specs_menu.py``) — это ТЕКСТ подсказки
планировщику; их надёжность юнитом не проверишь, она закрывается живым
прогоном N=5 (g-044). Здесь НЕТ теста на «планировщик выбрал list_menu».
"""

from __future__ import annotations

import re

import pytest

from sreda.runtime.planner.schemas import Plan
from sreda.runtime.planner.validator import validate_plan
from sreda.services.composer import presenters as _p
from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL
from sreda.services.composer.presenters import render_display_text
from sreda.services.tool_schemas.housewife import ListMenuEmpty
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS, MIGRATED_TOOL_SPECS


_REGISTRY = {s.name: s for s in MIGRATED_TOOL_SPECS}


@pytest.fixture(autouse=True)
def _real_display_field_map():
    """``presenters._DISPLAY_FIELD_MAP`` — module-level singleton; фикстуры
    test_115_* оставляют его в ``{}`` (``set_display_field_map({})``), отчего
    ``render_display_text("list_menu","empty")`` уходил бы в deny/«поломку» при
    определённом порядке тестов. Явно строим карту из ALL_TOOL_SPECS, затем
    сбрасываем в None — пусть другие модули лениво пересоберут (паттерн
    test_143_checklist_by_id / test_presenters; feedback_pytest_monkeypatch_required)."""
    _p.set_display_field_map(_p.build_display_field_map(ALL_TOOL_SPECS))
    try:
        yield
    finally:
        _p._DISPLAY_FIELD_MAP = None  # пусть другие модули лениво пересоберут

# Шаблон без контракта template_data — изолирует тест от композер-контрактов;
# проверяем ИМЕННО арг-ссылку правки, а не рендер веток.
_NO_CONTRACT_COMPOSE = {
    "kind": "template",
    "template_id": "generic_tool_error",
    "template_data": {},
}


def _canonical_edit_plan(plan_id_ref: str) -> Plan:
    """Канонический edit-план #144-A: s1=list_menu → s2=update_menu_item.

    Ветки s1:
    - ``ok`` → ``next='s2'`` (показ нашёлся, правим);
    - ``empty`` → ТЕРМИНАЛЬНАЯ (``next=None``) — «не составлено. Составить?»
      (preflight: пустое меню НЕ ведёт в write);
    - ``error`` → терминальная.

    ``s2`` ссылается на поле продюсера через ``plan_id_ref`` (параметр
    теста: ``${s1.menu_id}`` — верно; ``${s1.plan_id}`` — неверно).
    """
    return Plan.model_validate({
        "schema_version": 1,
        "turn_classification": {
            "is_new_turn": True,
            "reason": "правка обеда — preflight через list_menu",
        },
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "list_menu",
                "args": {},
                "expected_outcomes": [
                    {"match": {"status": "ok"}, "next": "s2", "compose": None},
                    # empty → терминал, показ presenter-текста (не write)
                    {"match": {"status": "empty"}, "next": None,
                     "compose": dict(_NO_CONTRACT_COMPOSE)},
                    {"match": {"status": "error"}, "next": None,
                     "compose": dict(_NO_CONTRACT_COMPOSE)},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
            "s2": {
                "tool": "update_menu_item",
                "args": {
                    "plan_id": plan_id_ref,
                    "day_of_week": 2,
                    "meal_type": "lunch",
                    "free_text": "суп",
                },
                "expected_outcomes": [
                    {"match": {"status": "updated"}, "next": None,
                     "compose": dict(_NO_CONTRACT_COMPOSE)},
                    {"match": {"status": "error"}, "next": None,
                     "compose": dict(_NO_CONTRACT_COMPOSE)},
                ],
                "intent_group": "default",
                "depends_on": ["s1"],
            },
        },
        "compose": dict(_NO_CONTRACT_COMPOSE),
    })


# ---------------------------------------------------------------------------
# Критерий приёмки #4 — правка через правильное поле выдачи list_menu
# ---------------------------------------------------------------------------


def test_canonical_edit_plan_with_menu_id_ref_passes_validator() -> None:
    """ПРАВИЛЬНАЯ форма правки: ``update_menu_item(plan_id="${s1.menu_id}")``
    после ``list_menu`` проходит ``validate_plan`` без нарушений.

    ``${s1.menu_id}`` проверяется branch-aware: ветка ``ok`` продюсера
    маршрутизирует в s2, поэтому ссылка валидируется против варианта
    ``ListMenuOk`` (где поле ``menu_id`` есть)."""
    plan = _canonical_edit_plan("${s1.menu_id}")
    violations = validate_plan(plan, registry=_REGISTRY)
    assert violations == [], (
        "канонический edit-план (plan_id=${s1.menu_id}) должен быть "
        f"валиден; нарушения: {[(v.code, v.message) for v in violations]}"
    )


def test_canonical_edit_plan_with_plan_id_ref_is_rejected() -> None:
    """НЕВЕРНОЕ поле: ``update_menu_item(plan_id="${s1.plan_id}")``.

    Поле выдачи list_menu называется ``menu_id``, поля ``plan_id`` в нём
    нет — валидатор C (#143) обязан отклонить ссылку с кодом
    ``arg_ref_unknown_field``. Закрепляет риск рассогласования
    ``menu_id`` / ``plan_id`` из плана #144-A."""
    plan = _canonical_edit_plan("${s1.plan_id}")
    violations = validate_plan(plan, registry=_REGISTRY)
    codes = [v.code for v in violations]
    assert "arg_ref_unknown_field" in codes, (
        "ссылка ${s1.plan_id} должна быть отклонена (plan_id нет в выдаче "
        f"list_menu); нарушения: {[(v.code, v.message) for v in violations]}"
    )
    # И сообщение должно называть реальное поле menu_id, чтобы ретрай-
    # фидбек вёл планировщик к правильной форме.
    msg = " ".join(
        v.message for v in violations if v.code == "arg_ref_unknown_field"
    )
    assert "menu_id" in msg, (
        f"сообщение о нарушении должно подсказать поле menu_id: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Критерий приёмки #1 — пустой показ через presenter, без дрейфа
# ---------------------------------------------------------------------------


def test_empty_menu_presenter_renders_display_summary() -> None:
    """Пустой исход list_menu рендерится через УЖЕ существующий
    ``ListMenuEmpty.display_summary`` (НЕ новый шаблон menu_empty):

    - текст РАВЕН ``display_summary`` (привязка к модели, не к ручной
      строке — анти-дрейф);
    - содержит предложение действия «Составить?»;
    - НЕ из пула «поломок» (``BREAKDOWN_POOL``) — это нормальный исход,
      не отказ;
    - не содержит внутренних id (``menu_``/``rec_``/``mpi_`` + hex)."""
    empty = ListMenuEmpty()
    rendered = render_display_text(
        "list_menu", empty.model_dump(), domain_status="empty"
    )

    assert rendered == empty.display_summary, (
        "пустой показ должен рендериться канонической строкой "
        f"display_summary; got {rendered!r}"
    )
    assert "Составить?" in rendered, (
        f"пустое меню должно звать составить («Составить?»): {rendered!r}"
    )
    assert rendered not in BREAKDOWN_POOL, (
        "пустой показ — нормальный исход, НЕ «поломка» из пула"
    )
    assert not re.search(r"(menu_|rec_|mpi_)[0-9a-f]", rendered), (
        f"в пустом показе не должно быть внутренних id: {rendered!r}"
    )


def test_empty_menu_display_summary_is_canonical_text() -> None:
    """Прямая проверка канонического текста (А1): «Составить?» уже
    добавлено в #144-A — карточка/фоллбэк ведут пустой показ именно
    сюда, без дублирующего шаблона."""
    assert (
        ListMenuEmpty().display_summary
        == "Меню на эту неделю ещё не составлено. Составить?"
    )
