"""#349: окно гейта времени (#180/#288) на слот-серии.

Прод 2026-07-11 12:35 (владелец): «Поставь напоминание с 13:30 каждый час» →
слот-исход → «до 18» → гейт НЕ видел «13:30» (окно = только последний
HumanMessage) → time_not_specified → лишние переспросы «Во сколько?».

Фикс: при слот-цепочке (между сообщениями юзера был исход time_not_specified)
окно расширяется на предыдущие сообщения серии - структурный признак (artifact),
не текст. Анти-регресс: новая тема (без слот-цепочки) окно НЕ расширяет
(«время события» из чужого хода не протекает в гейт).
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime.react_loop import _turn_time_window_text


def _series():
    return [
        HumanMessage(content="Поставь напоминание с 13:30 каждый час"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="уточни время", name="schedule_reminder", tool_call_id="t1",
                    artifact={"result_kind": "time_not_specified"}),
        AIMessage(content="До какого времени повторять?"),
        HumanMessage(content="до 18"),
    ]


def test_window_includes_series_time_349():
    """«13:30» из первого сообщения серии видно гейту на ходе «до 18»."""
    w = _turn_time_window_text(_series())
    assert "13:30" in w
    assert "до 18" in w


def test_window_no_expansion_without_slot_chain_349():
    """Без слот-цепочки (прошлый ход закрыт ok) окно = только последний Human -
    время «в 10» из ПРОШЛОГО закрытого хода не протекает."""
    msgs = [
        HumanMessage(content="напомни про визит в 10"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="ok:scheduled:rem_1:x", name="schedule_reminder", tool_call_id="t1",
                    artifact={"result_kind": "ok"}),
        AIMessage(content="Готово!"),
        HumanMessage(content="и про лекарства напомни"),
    ]
    w = _turn_time_window_text(msgs)
    assert "в 10" not in w
    assert "лекарства" in w


def test_window_multi_step_series_349():
    """Серия из трёх сообщений (слот дважды) - всё окно накапливается."""
    msgs = _series() + [
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t2"}]),
        ToolMessage(content="уточни", name="schedule_reminder", tool_call_id="t2",
                    artifact={"result_kind": "time_not_specified"}),
        AIMessage(content="Как назвать?"),
        HumanMessage(content="тест"),
    ]
    w = _turn_time_window_text(msgs)
    assert "13:30" in w and "до 18" in w and "тест" in w
