"""ФИКС #144-A — пост-фоллбэк композера «поломка → показ инструмента».

Подтверждённая причина (живой стенд): «покажи меню на эту неделю» на ПУСТОМ
меню → ответ «поломка». Исполнитель отработал (list_menu → ListMenuEmpty,
status=empty), но компоновка показа построена «в расчёте на непустое меню» и
свелась к «поломке», хотя у ListMenuEmpty уже есть дружелюбный display_summary.

Дизайн A1 (общий фоллбэк, см. compose._maybe_presenter_display_fallback):
если итоговая компоновка == «поломка», а у шага, на который ССЫЛАЕТСЯ
эффективная компоновка (executor-ok + доменный статус ok/empty), есть показ
через #141-презентер, отдаём показ вместо «поломки».

R1 фиксы код-ревью (Codex high+medium, конвергентно):
- M1 [MAJOR]: фоллбэк только при ``execution_log.outcome == "completed"`` —
  на partial_failure/aborted_partial ранний ok/empty-шаг НЕ маскирует провал.
- M2 [MAJOR]: target-шаг выбирается по РЕФАМ эффективной компоновки (а не по
  «последнему ok/empty»); кандидат должен быть РОВНО ОДИН.
- MINOR: origin-признак — «поломка» должна прийти реальным поломочным путём
  (шаблон generic_tool_error/invalid_plan_fallback или провал llm-композера),
  а не случайным совпадением строки с пулом.

RED-before-impl: тесты фиксируют именно НАБЛЮДАЕМЫЙ исход (текст показа, НЕ
«поломка», fallback_used выставлен), границы безопасности (error остаётся
поломкой; провальный outcome остаётся поломкой; не тот шаг не показывается;
успешная компоновка не трогается) и копи-правку ListMenuEmpty.
"""

from __future__ import annotations

import pytest

from sreda.runtime.planner.executor import ExecutionLog, StepResult
from sreda.runtime.planner.schemas import ComposerCall
from sreda.services.composer import presenters as _p
from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL
from sreda.services.composer.compose import (
    PRESENTER_DISPLAY_FALLBACK_COUNTS,
    ComposeResult,
    compose,
)
from sreda.services.tool_schemas.housewife import ListMenuEmpty, ListShoppingEmpty
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS


@pytest.fixture(autouse=True)
def _real_display_field_map():
    """``presenters._DISPLAY_FIELD_MAP`` — module-level singleton; фикстуры
    test_115_* оставляют его в ``{}`` (``set_display_field_map({})``), отчего
    ``render_display_text`` для пустого показа уходил бы в deny/«поломку» при
    определённом порядке тестов и #144-A фоллбэк не срабатывал. Явно строим
    карту из ALL_TOOL_SPECS, затем сбрасываем в None — пусть другие модули
    лениво пересоберут (паттерн test_143_checklist_by_id / test_presenters;
    см. feedback_pytest_monkeypatch_required)."""
    _p.set_display_field_map(_p.build_display_field_map(ALL_TOOL_SPECS))
    try:
        yield
    finally:
        _p._DISPLAY_FIELD_MAP = None  # пусть другие модули лениво пересоберут


# ---------------------------------------------------------------------------
# Helpers — повторяют шейпы из tests/unit/test_composer_compose.py
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
    """list_menu, исполнитель=ok, доменный статус=empty (ListMenuEmpty).

    parsed_output собирается ровно как в исполнителе: ``model_dump`` варианта,
    включая @computed_field display_summary."""
    return _step(
        step_id=step_id,
        tool="list_menu",
        status="ok",
        parsed_output=ListMenuEmpty().model_dump(),
    )


def _empty_shopping_step(step_id: str = "s1") -> StepResult:
    """list_shopping, исполнитель=ok, доменный статус=empty (ListShoppingEmpty).

    #144 (Задача 1): механизм пост-фоллбэка #144-A теперь демонстрируется на
    НЕ-меню инструменте, потому что для list_menu empty действует более сильный
    ДЕТЕРМИНИРОВАННЫЙ override (см. test_144_menu_empty_override.py), который
    перекрыл бы #144-A. list_shopping empty — displayable, не-меню, не из пула —
    идеален, чтобы изолированно проверять именно #144-A."""
    return _step(
        step_id=step_id,
        tool="list_shopping",
        status="ok",
        parsed_output=ListShoppingEmpty().model_dump(),
    )


# Текст-показ пустого меню (то, что должно уйти пользователю вместо «поломки»).
_EMPTY_MENU_DISPLAY = "Меню на эту неделю ещё не составлено. Составить?"

# Текст-показ пустого списка покупок (для изолированных #144-A кейсов).
_EMPTY_SHOPPING_DISPLAY = "Список покупок пуст."


# generic_tool_error рендерит фразу-«поломку» из пула (error_code не печатается);
# чтобы эффективная компоновка СТРУКТУРНО ссылалась на s1 (M2), кладём в неё
# резолвящийся диагностический реф на поле выдачи s1 (status у пустого меню
# есть, error_code — нет; реф ОБЯЗАН резолвиться, иначе compose_error вместо
# generic_tool_error). Это и есть форма из few-shot (error_code=${sN.field}).
def _breakdown_call_ref_s1(step_id: str = "s1") -> ComposerCall:
    return _template_call(
        "generic_tool_error", {"error_code": "${%s.status}" % step_id},
    )


# ---------------------------------------------------------------------------
# Основной кейс #144 — «поломка» на пустом меню → показ инструмента
# ---------------------------------------------------------------------------


def test_breakdown_with_empty_step_falls_back_to_presenter_display() -> None:
    """compose() свёлся к «поломке» (generic_tool_error рендерит breakdown_phrase),
    эффективная компоновка ссылается на s1, шаг = list_shopping со статусом empty
    (ListShoppingEmpty) → итог = display_summary «Список покупок пуст.», НЕ
    «поломка», fallback_used=presenter_display_fallback.

    #144 (Задача 1): кейс переведён с list_menu на list_shopping, т.к. для
    пустого меню действует более сильный override (другой файл тестов)."""
    call = _breakdown_call_ref_s1("s1")
    log = _log([_empty_shopping_step("s1")])

    res = compose(call, log)

    assert isinstance(res, ComposeResult)
    assert res.text == _EMPTY_SHOPPING_DISPLAY
    assert res.text not in BREAKDOWN_POOL
    assert res.fallback_used == "presenter_display_fallback"


def test_presenter_fallback_bumps_observability_counter() -> None:
    """Счётчик наблюдаемости PRESENTER_DISPLAY_FALLBACK_COUNTS растёт по
    имени инструмента при срабатывании фоллбэка."""
    before = PRESENTER_DISPLAY_FALLBACK_COUNTS.get("list_shopping", 0)
    res = compose(_breakdown_call_ref_s1("s1"), _log([_empty_shopping_step("s1")]))
    assert res.fallback_used == "presenter_display_fallback"
    after = PRESENTER_DISPLAY_FALLBACK_COUNTS.get("list_shopping", 0)
    assert after == before + 1


# ---------------------------------------------------------------------------
# M1 [MAJOR] — гейт outcome == "completed"
# ---------------------------------------------------------------------------


def test_m1_no_fallback_on_partial_failure_outcome() -> None:
    """План с outcome=partial_failure: ранний ok/empty list_menu-шаг есть и
    эффективная компоновка на него ссылается, НО компоновка ЧЕСТНО ушла в
    «поломку» (реальный провал плана) → фоллбэк НЕ срабатывает, итог остаётся
    в BREAKDOWN_POOL. Иначе ранний шаг замаскировал бы реальный сбой."""
    call = _breakdown_call_ref_s1("s1")
    log = _log([_empty_menu_step("s1")], outcome="partial_failure")
    res = compose(call, log)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "presenter_display_fallback"


def test_m1_no_fallback_on_aborted_partial_outcome() -> None:
    """То же, что M1, но outcome=aborted_partial — другой провальный исход:
    фоллбэк НЕ срабатывает."""
    call = _breakdown_call_ref_s1("s1")
    log = _log([_empty_menu_step("s1")], outcome="aborted_partial")
    res = compose(call, log)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "presenter_display_fallback"


# ---------------------------------------------------------------------------
# M2 [MAJOR] — выбор шага по РЕФАМ эффективной компоновки, не «последний ok»
# ---------------------------------------------------------------------------


def test_m2_does_not_show_wrong_step_when_ref_points_at_other_step() -> None:
    """completed-план, ДВА успешных шага: s1 (list_shopping empty, displayable) и
    s2 (другой инструмент, ok, тоже displayable). Эффективная компоновка ссылается
    на s1 → фоллбэк показывает ИМЕННО s1 (покупки), НЕ последний displayable s2.
    Без M2 (старое «последний ok/empty») показали бы s2 — неверный ответ."""
    log = _log([
        _empty_shopping_step("s1"),
        # поздний успешный шаг другого инструмента — НЕ должен победить
        _step("s2", tool="list_reminders", status="ok",
              parsed_output={"status": "empty"}),
    ])
    call = _breakdown_call_ref_s1("s1")  # ссылка только на s1
    res = compose(call, log)
    assert res.fallback_used == "presenter_display_fallback"
    assert res.text == _EMPTY_SHOPPING_DISPLAY
    assert res.text not in BREAKDOWN_POOL


def test_m2_single_referenced_step_fires_fallback() -> None:
    """Обратный кейс: РОВНО ОДИН реф-шаг (s2), он же единственный displayable
    кандидат → фоллбэк срабатывает на него (s2 = покупки), хотя в логе есть и s1."""
    log = _log([
        _step("s1", tool="list_reminders", status="ok",
              parsed_output={"status": "empty"}),
        _empty_shopping_step("s2"),
    ])
    call = _breakdown_call_ref_s1("s2")  # ссылка только на s2 (покупки)
    res = compose(call, log)
    assert res.fallback_used == "presenter_display_fallback"
    assert res.text == _EMPTY_SHOPPING_DISPLAY


def test_m2_no_fallback_when_referenced_step_not_displayable() -> None:
    """Эффективная компоновка ссылается на s1 (доменный статус error — реальная
    ошибка, приехавшая ok-шагом, НЕ displayable). В логе есть displayable s2, но
    компоновка на него НЕ ссылается → кандидатов 0 → фоллбэк НЕ срабатывает (не
    показываем не тот шаг и не маскируем доменную ошибку)."""
    log = _log([
        # s1 — ok по executor, но доменный статус error: displayable=False.
        _step("s1", tool="list_menu", status="ok",
              parsed_output={"status": "error", "error_code": "boom"}),
        _empty_menu_step("s2"),  # displayable, но компоновка на него не ссылается
    ])
    call = _breakdown_call_ref_s1("s1")  # реф на s1.status → резолвится в "error"
    res = compose(call, log)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "presenter_display_fallback"


def test_m2_no_fallback_when_two_referenced_displayable_candidates() -> None:
    """Эффективная компоновка ссылается на ДВА шага, оба displayable
    (ok/empty) → кандидатов 2 → неоднозначно → фоллбэк НЕ срабатывает.

    #144 (Задача 1): шаги — НЕ list_menu (иначе сработал бы более сильный
    menu-override на первом реф-шаге); берём list_shopping + list_reminders."""
    log = _log([
        _empty_shopping_step("s1"),
        _step("s2", tool="list_reminders", status="ok",
              parsed_output={"status": "empty"}),
    ])
    # реф на оба: s1.status И s2.status (оба резолвятся в "empty")
    call = _template_call(
        "generic_tool_error",
        {"a": "${s1.status}", "b": "${s2.status}"},
    )
    res = compose(call, log)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "presenter_display_fallback"


def test_m2_no_fallback_when_compose_references_no_step() -> None:
    """Эффективная компоновка вообще без ${sN}-рефов (literal error_code) →
    referenced пуст → нечем заякорить выбор → фоллбэк НЕ срабатывает, даже если
    в логе есть displayable list_menu empty."""
    call = _template_call("generic_tool_error", {"error_code": "x"})
    log = _log([_empty_menu_step("s1")])
    res = compose(call, log)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "presenter_display_fallback"


# ---------------------------------------------------------------------------
# MINOR — origin-признак (не только текст)
# ---------------------------------------------------------------------------


def test_minor_no_fallback_when_template_not_breakdown_origin() -> None:
    """Текст СЛУЧАЙНО совпал с фразой пула, но эффективный шаблон НЕ из
    {generic_tool_error, invalid_plan_fallback} и fallback_used != generic_error
    → за «поломку» не принимаем, фоллбэк НЕ срабатывает.

    Воспроизводим через шаблон recipe_show, который печатает recipe_text как
    есть: кладём в recipe_text фразу из пула + реф на s1 → текст == «поломка»,
    но origin легитимный (успешный рендер не-поломочного шаблона).

    #144 (Задача 1): s1 — list_shopping empty, НЕ list_menu (для меню сработал бы
    более сильный override, который НЕ смотрит на origin)."""
    pool_phrase = BREAKDOWN_POOL[0]
    call = _template_call(
        "recipe_show",
        {"recipe_text": pool_phrase, "_anchor": "${s1.status}"},
    )
    log = _log([_empty_shopping_step("s1")])
    res = compose(call, log)
    # текст == фраза пула, НО фоллбэк не сработал (origin не поломочный)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "presenter_display_fallback"


# ---------------------------------------------------------------------------
# Границы безопасности — НЕ маскировать реальные сбои (доменный статус)
# ---------------------------------------------------------------------------


def test_no_fallback_when_domain_status_is_error() -> None:
    """Доменный статус реф-шага вне {ok, empty} (реальная доменная ошибка,
    приехавшая ok-шагом) → «поломка» остаётся, не маскируем."""
    call = _breakdown_call_ref_s1("s1")
    log = _log([
        _step("s1", tool="list_menu", status="ok",
              parsed_output={"status": "error", "error_code": "boom"}),
    ])
    res = compose(call, log)
    assert res.text in BREAKDOWN_POOL
    assert res.fallback_used != "presenter_display_fallback"


# ---------------------------------------------------------------------------
# НЕ трогаем успешную (не-поломочную) компоновку
# ---------------------------------------------------------------------------


def test_successful_compose_is_left_untouched() -> None:
    """Успешная (не-поломочная) компоновка остаётся как есть — даже если в
    логе есть ok/empty-шаг с доступным показом. Фоллбэк срабатывает ТОЛЬКО на
    тексте-«поломке»."""
    call = _template_call("shopping_added_ok", {"items": ["молоко", "хлеб"]})
    log = _log([
        _step("s1", tool="add_shopping_items", status="ok",
              parsed_output={"status": "added"}),
        _empty_menu_step("s2"),  # есть ok/empty-шаг, но компоновка успешна
    ])
    res = compose(call, log)
    assert res.text == "Записала: молоко, хлеб."
    assert res.fallback_used is None


def test_compose_error_path_is_not_a_breakdown_and_untouched() -> None:
    """partial_with_compose_error (НЕ «поломка» — содержательный текст «Сделала
    что просила…») не подменяется показом: фоллбэк смотрит только на членство
    в пуле «поломок»."""
    # template_data без обязательного поля → template_render_error →
    # compose_error (текст «Сделала что просила…»), это НЕ фраза из пула.
    call = _template_call("shopping_added_ok", {})
    log = _log([_empty_menu_step("s1")])
    res = compose(call, log)
    assert res.fallback_used == "compose_error"
    assert res.text not in BREAKDOWN_POOL
    assert "Сделала что просила" in res.text


# ---------------------------------------------------------------------------
# Копи-правка ListMenuEmpty (#144 — Boris прямо просил «Составить?»)
# ---------------------------------------------------------------------------


def test_list_menu_empty_display_summary_offers_to_create() -> None:
    """ListMenuEmpty.display_summary содержит предложение действия «Составить?»."""
    summary = ListMenuEmpty().display_summary
    assert "Составить?" in summary
    assert summary == _EMPTY_MENU_DISPLAY
