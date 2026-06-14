"""ФИКС #144 (Задача 1) — ДЕТЕРМИНИРОВАННЫЙ override пустого list_menu.

Подтверждённая причина (живой прогон, N=5): «покажи меню»/«поменяй обед» на
ПУСТОМ меню. ``list_menu`` ВЫЗЫВАЕТСЯ (executor ok, доменный статус ``empty``),
НО компоновка ответа планировщика для пустого результата ДИКО ненадёжна — даёт
«Не нашла рецепт „меню“» (мисроут), «Твой список покупок пуст» (мисроут), «Ой…
заело» (поломка), и лишь иногда верное «Меню не составлено».

Композер-фоллбэк #144-A (_maybe_presenter_display_fallback) ловит ТОЛЬКО текст-
«поломку» (BREAKDOWN_POOL), но НЕ «успешные-но-неверные» компоновки (рецепт/
покупки). Этот override СИЛЬНЕЕ: для пустого меню «не составлено» — ВСЕГДА
правильный ответ, а любая иная компоновка — мисроут, поэтому override перекрывает
ЛЮБУЮ компоновку планировщика (рецепт / покупки / поломку).

Узость/безопасность (гейты):
- ТОЛЬКО ``tool == "list_menu"``;
- ТОЛЬКО доменный статус ``empty`` (``parsed_output["status"] == "empty"``);
- ТОЛЬКО executor-статус шага ``ok``;
- ТОЛЬКО ``execution_log.outcome == "completed"``.
Реальные сбои (executor error/timeout, доменный error) НЕ трогаем.

RED-before-impl (4 машинно-проверяемых пункта из задачи):
(а) execution_log с list_menu domain-empty + компоновка-мисроут «Не нашла
    рецепт» → итог = ListMenuEmpty.display_summary (НЕ мисроут);
(б) то же при компоновке-«поломке»;
(в) list_menu со статусом ok (непустой) → НЕ override (показ как был);
(г) другой инструмент empty (list_shopping empty) → НЕ затронут menu-override.
"""

from __future__ import annotations

import pytest

from sreda.runtime.planner.executor import ExecutionLog, StepResult
from sreda.runtime.planner.schemas import ComposerCall
from sreda.services.composer import presenters as _p
from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL
from sreda.services.composer.compose import (
    MENU_EMPTY_OVERRIDE_COUNTS,
    ComposeResult,
    compose,
)
from sreda.services.tool_schemas.housewife import ListMenuEmpty
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS


@pytest.fixture(autouse=True)
def _real_display_field_map():
    """``presenters._DISPLAY_FIELD_MAP`` — module-level singleton; фикстуры
    test_115_* оставляют его в ``{}`` в teardown (``set_display_field_map({})``),
    из-за чего ``render_display_text("list_menu","empty")`` без этой страховки
    уходил бы в deny/«поломку» при определённом порядке тестов, и override
    (который зовёт презентер) не сработал бы. Явно строим карту из
    ALL_TOOL_SPECS, затем сбрасываем в None — пусть другие модули лениво
    пересоберут (паттерн test_143_checklist_by_id / test_presenters;
    см. feedback_pytest_monkeypatch_required)."""
    _p.set_display_field_map(_p.build_display_field_map(ALL_TOOL_SPECS))
    try:
        yield
    finally:
        _p._DISPLAY_FIELD_MAP = None  # пусть другие модули лениво пересоберут


# Канонический текст пустого меню — то, что override обязан вернуть.
_EMPTY_MENU_DISPLAY = "Меню на эту неделю ещё не составлено. Составить?"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _step(
    step_id: str = "s1",
    tool: str = "list_menu",
    *,
    status: str = "ok",
    parsed_output: dict | None = None,
) -> StepResult:
    return StepResult(
        step_id=step_id,
        tool=tool,
        status=status,  # type: ignore[arg-type]
        parsed_output=parsed_output,
    )


def _log(steps: list[StepResult], outcome: str = "completed") -> ExecutionLog:
    return ExecutionLog(steps=tuple(steps), outcome=outcome)  # type: ignore[arg-type]


def _template_call(template_id: str, data: dict | None = None) -> ComposerCall:
    return ComposerCall(
        kind="template", template_id=template_id, template_data=data or {},
    )


def _empty_menu_step(step_id: str = "s1") -> StepResult:
    """list_menu, executor=ok, доменный статус=empty (ListMenuEmpty)."""
    return _step(
        step_id=step_id,
        tool="list_menu",
        status="ok",
        parsed_output=ListMenuEmpty().model_dump(),
    )


# Компоновка-МИСРОУТ «успешная, но неверная»: recipe_show печатает recipe_text
# как есть → текст НЕ из пула «поломок» (fallback_used=None), как мисроут на проде
# («Не нашла рецепт „меню"»). #144-A такой случай НЕ ловит — а override обязан.
#
# Якорь по рефам (как у #144-A): компоновка СТРУКТУРНО ссылается на s1 через
# резолвящийся ``${s1.status}`` (у пустого меню поле status есть), что и
# означает «компоновка собиралась строить ответ ИЗ пустого меню».
_MISROUTE_RECIPE_TEXT = "Не нашла рецепт «меню»."


def _misroute_recipe_call(step_id: str = "s1") -> ComposerCall:
    return _template_call(
        "recipe_show",
        {"recipe_text": _MISROUTE_RECIPE_TEXT, "_anchor": "${%s.status}" % step_id},
    )


# Компоновка-«поломка»: generic_tool_error рендерит фразу из BREAKDOWN_POOL.
# error_code=${s1.status} → резолвится (поле status у пустого меню есть) и якорит
# компоновку на s1.
def _breakdown_call(step_id: str = "s1") -> ComposerCall:
    return _template_call(
        "generic_tool_error", {"error_code": "${%s.status}" % step_id},
    )


# ---------------------------------------------------------------------------
# (а) мисроут-компоновка (успешная, но неверная) → override перекрывает
# ---------------------------------------------------------------------------


def test_a_empty_menu_overrides_successful_misroute_compose() -> None:
    """list_menu domain-empty + компоновка свелась к мисроуту «Не нашла рецепт»
    (успешный рендер recipe_show, НЕ поломка) → итог = ListMenuEmpty.display_summary.

    Это ядро Задачи 1: override СИЛЬНЕЕ #144-A — срабатывает на УСПЕШНОЙ
    (не-поломочной) компоновке, чего пост-фоллбэк #144-A не умеет."""
    res = compose(_misroute_recipe_call(), _log([_empty_menu_step("s1")]))

    assert isinstance(res, ComposeResult)
    # без override итог был бы мисроут-рецептом
    assert res.text == _EMPTY_MENU_DISPLAY
    assert res.text != _MISROUTE_RECIPE_TEXT
    assert res.fallback_used == "menu_empty_override"


def test_a_empty_menu_overrides_shopping_misroute_compose() -> None:
    """Второй прод-мисроут: компоновка свелась к «Твой список покупок пуст»
    (успешный рендер shopping-показа), хотя шаг — пустое меню → override."""
    call = _template_call(
        "recipe_show",
        {"recipe_text": "Твой список покупок пуст.", "_anchor": "${s1.status}"},
    )
    res = compose(call, _log([_empty_menu_step("s1")]))
    assert res.text == _EMPTY_MENU_DISPLAY
    assert res.fallback_used == "menu_empty_override"


# ---------------------------------------------------------------------------
# (б) компоновка-«поломка» → override перекрывает
# ---------------------------------------------------------------------------


def test_b_empty_menu_overrides_breakdown_compose() -> None:
    """list_menu domain-empty + компоновка свелась к «поломке» (generic_tool_error)
    → итог = ListMenuEmpty.display_summary, НЕ «поломка»."""
    res = compose(_breakdown_call(), _log([_empty_menu_step("s1")]))
    assert res.text == _EMPTY_MENU_DISPLAY
    assert res.text not in BREAKDOWN_POOL
    assert res.fallback_used == "menu_empty_override"


# ---------------------------------------------------------------------------
# (в) list_menu НЕ пустой (status ok) → НЕ override
# ---------------------------------------------------------------------------


def test_c_non_empty_list_menu_is_not_overridden() -> None:
    """list_menu со статусом ok (непустое меню) → override НЕ срабатывает,
    показ остаётся как был (успешная компоновка не трогается)."""
    ok_menu = _step(
        "s1", tool="list_menu", status="ok",
        parsed_output={
            "status": "ok",
            "menu_id": "menu_" + "a" * 24,
            "week_start_iso": "2026-06-15",
            "raw_text": "menu_id: menu_x\nweek_start: 2026-06-15\nПн: суп",
        },
    )
    # успешная компоновка показа меню
    call = _template_call("recipe_show", {"recipe_text": "Пн: суп"})
    res = compose(call, _log([ok_menu]))
    assert res.text == "Пн: суп"
    assert res.fallback_used is None


# ---------------------------------------------------------------------------
# (г) другой инструмент empty (list_shopping) → НЕ затронут menu-override
# ---------------------------------------------------------------------------


def test_d_other_tool_empty_is_not_menu_overridden() -> None:
    """list_shopping со статусом empty (НЕ меню) + успешная компоновка →
    menu-override НЕ затрагивает (он специфичен ТОЛЬКО для list_menu)."""
    call = _template_call("recipe_show", {"recipe_text": "Список покупок пуст."})
    log = _log([
        _step("s1", tool="list_shopping", status="ok",
              parsed_output={"status": "empty"}),
    ])
    res = compose(call, log)
    assert res.text == "Список покупок пуст."
    assert res.fallback_used is None


# ---------------------------------------------------------------------------
# Границы безопасности — реальные сбои НЕ маскируем
# ---------------------------------------------------------------------------


def test_safety_unreferenced_empty_menu_step_not_overridden() -> None:
    """🔴 КЛЮЧЕВАЯ граница: пустой list_menu есть в логе, но компоновка на него
    НЕ ссылается (интент НЕ про меню — напр. «купи молоко» с попутным preflight'ом
    меню). Override НЕ срабатывает — иначе слепо испортил бы верный ответ про
    покупки. Якорь по рефам — единственная защита от этого ложного срабатывания."""
    log = _log([
        # успешная покупка — это и есть ответ пользователю
        _step("s1", tool="add_shopping_items", status="ok",
              parsed_output={"status": "added"}),
        # попутный пустой menu-preflight, на который компоновка НЕ ссылается
        _empty_menu_step("s2"),
    ])
    # успешная компоновка про покупки, без рефа на s2 (меню)
    call = _template_call("shopping_added_ok", {"items": ["молоко"]})
    res = compose(call, log)
    assert res.text == "Записала: молоко."
    assert res.fallback_used is None


# Литеральная «поломка» без рефов — для error-safety кейсов, где шаг провалился
# и ${sN.status} не зарезолвился бы (compose_error вместо breakdown).
def _breakdown_call_literal() -> ComposerCall:
    return _template_call("generic_tool_error", {"error_code": "x"})


def test_safety_executor_error_step_not_overridden() -> None:
    """list_menu с executor-статусом error (реальный сбой исполнения) →
    override НЕ срабатывает, «поломка» остаётся честной."""
    log = _log([
        _step("s1", tool="list_menu", status="error", parsed_output=None),
    ])
    res = compose(_breakdown_call_literal(), log)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "menu_empty_override"


def test_safety_domain_error_menu_not_overridden() -> None:
    """list_menu executor-ok, но доменный статус error (доменная ошибка,
    приехавшая ok-шагом) → НЕ override, не маскируем доменную ошибку."""
    log = _log([
        _step("s1", tool="list_menu", status="ok",
              parsed_output={"status": "error", "error_code": "boom"}),
    ])
    # реф на s1.status резолвится в "error" → generic_tool_error (breakdown)
    res = compose(_breakdown_call("s1"), log)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "menu_empty_override"


def test_safety_non_completed_outcome_not_overridden() -> None:
    """Провальный outcome (partial_failure): пустое меню есть, но план честно
    провалился → override НЕ срабатывает (не маскируем реальный сбой плана)."""
    log = _log([_empty_menu_step("s1")], outcome="partial_failure")
    # на провальном outcome _pick_effective_call игнорирует terminal-override'ы,
    # но plan-call здесь и есть наш generic_tool_error с рефом на s1.
    res = compose(_breakdown_call("s1"), log)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "menu_empty_override"


# ---------------------------------------------------------------------------
# наблюдаемость — счётчик
# ---------------------------------------------------------------------------


def test_override_bumps_observability_counter() -> None:
    """MENU_EMPTY_OVERRIDE_COUNTS["list_menu"] растёт при срабатывании."""
    before = MENU_EMPTY_OVERRIDE_COUNTS.get("list_menu", 0)
    res = compose(_misroute_recipe_call(), _log([_empty_menu_step("s1")]))
    assert res.fallback_used == "menu_empty_override"
    after = MENU_EMPTY_OVERRIDE_COUNTS.get("list_menu", 0)
    assert after == before + 1


# ---------------------------------------------------------------------------
# edit-flow: «поменяй обед» на пустом меню (preflight list_menu → empty)
# ---------------------------------------------------------------------------


def test_edit_flow_preflight_empty_menu_overridden() -> None:
    """Edit-план «поменяй обед»: s1=list_menu (preflight) вернул empty,
    ветка терминальная (план completed, write НЕ выполнялся). Компоновка
    свелась к «поломке»/мисроуту → override даёт дружелюбный «не составлено».
    Тот же путь: list_menu empty в логе + outcome completed."""
    log = _log([_empty_menu_step("s1")])
    res = compose(_breakdown_call(), log)
    assert res.text == _EMPTY_MENU_DISPLAY
    assert res.fallback_used == "menu_empty_override"
