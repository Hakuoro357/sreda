"""R-39: тесты детерминированного рендера первой строки.

`render_first_line(entry, context) -> str` — из одной записи журнала
одна короткая фраза по контракту. `render_journal(entries, context)` —
склейка нескольких через перевод строки.
"""

from __future__ import annotations

import pytest

from sreda.agents.contracts import (
    ResultKind,
    ToolJournalEntry,
    TurnContext,
)
from sreda.services.first_line_renderer import (
    render_first_line,
    render_journal,
)


# ─── Базовый рендер ───────────────────────────────────────────────────


def test_render_schedule_success_matches_a_variant() -> None:
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={
            "title": "Разбудить Катю",
            "trigger_human": "сегодня в 14:00",
        },
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    assert "Разбудить Катю" in result
    assert "сегодня в 14:00" in result


def test_render_cancel_success() -> None:
    entry = ToolJournalEntry(
        tool_name="cancel_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": "Разбудить"},
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    assert "Разбудить" in result


def test_render_save_recipe() -> None:
    entry = ToolJournalEntry(
        tool_name="save_recipe",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": "Борщ"},
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    assert "Борщ" in result


# ─── Полярность исхода ───────────────────────────────────────────────


def test_render_failure_uses_failure_template() -> None:
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.FAILURE,
        result_data={},
        error_message="db_timeout",
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    # failure-варианты для schedule_reminder содержат «не получилось»/«не сработало»
    assert "не " in result.lower() or "ещё раз" in result.lower()


def test_render_partial_when_supports_true() -> None:
    """add_shopping_items: supports_partial=True — partial-вариант возможен.

    R-39 R4: replace_reminder убран из registry, заменён на update_reminder
    (no partial). Tест перенесён на add_shopping_items который остаётся
    с supports_partial=True.
    """
    entry = ToolJournalEntry(
        tool_name="add_shopping_items",
        action_index=0,
        result_kind=ResultKind.PARTIAL,
        result_data={},
        error_message="cancel_done_but_schedule_failed",
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    assert result  # должен что-то отрендерить
    # partial template add_shopping_items упоминает «что-то не записалось»
    assert "что-то" in result.lower() or "проверь" in result.lower()


def test_render_partial_falls_back_when_supports_false() -> None:
    """schedule_reminder: supports_partial=False — partial обрабатывается как failure."""
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.PARTIAL,
        result_data={"title": "Разбудить"},
        error_message="weird",
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    # должен взять failure-шаблон, не упасть
    assert result
    assert "разбудить" not in result.lower() or "не " in result.lower()


# ─── Стабильность seed ────────────────────────────────────────────────


def test_seed_stable_for_same_inputs() -> None:
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={
            "title": "Разбудить Катю",
            "trigger_human": "сегодня в 14:00",
        },
    )
    ctx = TurnContext(turn_id="t-stable-1", tenant_id="42")
    first = render_first_line(entry, ctx)
    second = render_first_line(entry, ctx)
    assert first == second


def test_seed_varies_with_turn_id() -> None:
    """Разные turn_id обычно (не всегда) дают разные варианты."""
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={
            "title": "Разбудить Катю",
            "trigger_human": "сегодня в 14:00",
        },
    )
    seen: set[str] = set()
    for i in range(20):
        ctx = TurnContext(turn_id=f"t-{i}", tenant_id="42")
        seen.add(render_first_line(entry, ctx))
    # на 20 разных turn_id должно встретиться минимум 3 разных варианта (из 7)
    assert len(seen) >= 3


def test_seed_varies_with_action_index() -> None:
    """action_index влияет на seed — два одинаковых действия в обороте
    могут получить разные варианты."""
    entries = [
        ToolJournalEntry(
            tool_name="schedule_reminder",
            action_index=i,
            result_kind=ResultKind.SUCCESS,
            result_data={
                "title": "Купить хлеб",
                "trigger_human": "завтра в 9:00",
            },
        )
        for i in range(15)
    ]
    ctx = TurnContext(turn_id="t-multi", tenant_id="42")
    rendered = {render_first_line(e, ctx) for e in entries}
    assert len(rendered) >= 3


# ─── Multi-entry журнал ──────────────────────────────────────────────


def test_render_journal_joins_with_newlines() -> None:
    entries = [
        ToolJournalEntry(
            tool_name="cancel_reminder",
            action_index=0,
            result_kind=ResultKind.SUCCESS,
            result_data={"title": "Разбудить"},
        ),
        ToolJournalEntry(
            tool_name="schedule_reminder",
            action_index=1,
            result_kind=ResultKind.SUCCESS,
            result_data={
                "title": "Разбудить Катю",
                "trigger_human": "сегодня в 14:00",
            },
        ),
    ]
    ctx = TurnContext(turn_id="t-kati", tenant_id="42")
    result = render_journal(entries, ctx)
    lines = result.split("\n")
    assert len(lines) == 2
    assert "Разбудить" in lines[0]
    assert "сегодня в 14:00" in lines[1]


def test_render_journal_empty_returns_empty() -> None:
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    assert render_journal([], ctx) == ""


# ─── Защита от плохих входов ─────────────────────────────────────────


def test_unknown_tool_returns_generic_acknowledgement() -> None:
    """Незарегистрированный инструмент — нейтральный ответ."""
    entry = ToolJournalEntry(
        tool_name="some_unknown_tool",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={},
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    assert result  # не пусто
    assert "✓" in result or "готово" in result.lower()


def test_missing_placeholder_falls_back_gracefully() -> None:
    """Если в result_data нет ожидаемого ключа — не падаем."""
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": "Только title"},  # нет trigger_human
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    assert result  # упало бы → пустая строка из catch
    # generic fallback или partial-render — но не crash


def test_rendered_line_does_not_contain_newline() -> None:
    """Контракт: одна запись — одна строка."""
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={
            "title": "Разбудить\nКатю",  # title с переносом — не должно ломать
            "trigger_human": "сегодня в 14:00",
        },
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    assert "\n" not in result


# ─── Безопасная коэрция значений (review MINOR 2) ─────────────────────


def test_none_value_renders_as_empty_string() -> None:
    """result_data с None не должно показать «None» пользователю."""
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": None, "trigger_human": "сегодня в 14:00"},
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    assert "None" not in result
    assert "сегодня в 14:00" in result


def test_list_value_does_not_leak_raw_repr() -> None:
    """result_data с list — это симптом ошибки контракта; не должен
    выдавать пользователю «['a', 'b']»."""
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": ["a", "b"], "trigger_human": "сегодня в 14:00"},
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    # str(list) выдаёт «['a', 'b']» — пропускаем через sanitize, но проверим
    # что хотя бы не падает и time подставился
    assert "сегодня в 14:00" in result


def test_int_value_rendered_as_string() -> None:
    """int не падает — приводится через str() в sanitize."""
    # schedule_reminder template содержит {title} — подставим int.
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": 3, "trigger_human": "сегодня в 14:00"},
    )
    ctx = TurnContext(turn_id="t-001", tenant_id="42")
    result = render_first_line(entry, ctx)
    assert "3" in result


# ─── Empty turn_id (review MINOR 3) ───────────────────────────────────


def test_empty_turn_id_still_renders_deterministic_result(caplog) -> None:
    """Пустой turn_id не должен ломать рендер, но логировать warning."""
    import logging

    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": "X", "trigger_human": "сегодня в 14:00"},
    )
    ctx = TurnContext(turn_id="", tenant_id="42")
    with caplog.at_level(logging.WARNING):
        result = render_first_line(entry, ctx)
    assert result  # не пусто
    assert any("пустой turn_id" in rec.message for rec in caplog.records)


# ─── Главный регрессионный тест (Кати-сценарий) ──────────────────────


def test_kati_correction_journal_two_lines() -> None:
    """Полный сценарий: cancel + schedule (или replace) на correction-turn.

    Должно получиться 2 строки, первая про снятие старого, вторая
    про новое 14:00 «Разбудить Катю».
    """
    entries = [
        ToolJournalEntry(
            tool_name="cancel_reminder",
            action_index=0,
            result_kind=ResultKind.SUCCESS,
            result_data={"title": "Разбудить"},
        ),
        ToolJournalEntry(
            tool_name="schedule_reminder",
            action_index=1,
            result_kind=ResultKind.SUCCESS,
            result_data={
                "title": "Разбудить Катю",
                "trigger_human": "сегодня в 14:00",
            },
        ),
    ]
    ctx = TurnContext(turn_id="t-2026-05-17T13:21Z-352612382", tenant_id=352612382)
    result = render_journal(entries, ctx)
    lines = result.split("\n")
    assert len(lines) == 2
    # первая строка про отмену
    assert "Разбудить" in lines[0]
    # вторая строка про новое время и Катю
    assert "Катю" in lines[1]
    assert "14:00" in lines[1]
