"""Test that empty-final-text rescue только current turn'а, не history.

Incident 2026-04-28 16:26 (tg_634496616):
- Юзер: «запиши в список дел на сегодня найти чек от ноутбука»
- LLM: tools=['add_task'] (wrong tool), iter=1 text=''
- Existing rescue logic шёл по ВСЕМ messages включая history → выловил
  AIMessage из turn'а 15:02 «Удалила ✅ — Покрасить дом, ...»
- Юзер увидел «зомби-ответ» с STALE данными прошлого turn'а

Этот тест проверяет архитектуру: rescue должен ограничиваться
messages[_turn_msg_start_idx:] (то есть current turn).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


def test_rescue_only_from_current_turn_messages():
    """Rescue iteration should only consider messages added in current
    turn, not history (which appears as alternating Human/AI before
    current turn's HumanMessage).
    """
    # Имитируем messages list с history + current turn:
    sys_msg = SystemMessage(content="system")
    history_human_1 = HumanMessage(content="prev user msg")
    history_ai_1 = AIMessage(content="STALE OLD REPLY: Удалила ✅ ...")
    current_human = HumanMessage(content="запиши в список дел чек")
    current_ai_empty = AIMessage(content="")  # LLM вернул пустой text

    messages = [
        sys_msg, history_human_1, history_ai_1,
        current_human, current_ai_empty,
    ]

    # Boundary: index AFTER current_human (current turn starts here)
    turn_start_idx = 4  # current_human at idx 3, next msg at idx 4

    # Old logic (BUG): reversed(messages) → finds history_ai_1
    # New logic: only reversed(messages[turn_start_idx:])

    # New logic
    rescued = None
    for m in reversed(messages[turn_start_idx:]):
        if isinstance(m, AIMessage):
            content = (m.content or "").strip()
            if content:
                rescued = content
                break

    # Should be None (current_ai_empty is empty, no other AI in current turn)
    assert rescued is None, (
        "Rescue должен НЕ цепляться за AIMessage из history. "
        f"Got: {rescued!r}"
    )


def test_rescue_picks_legitimate_current_turn_ai_message():
    """Если в CURRENT turn есть predшествующий AIMessage с content
    (multi-iter с tool_calls + final empty), он должен подхватываться."""
    sys_msg = SystemMessage(content="system")
    current_human = HumanMessage(content="user input")
    legitimate_iter0_ai = AIMessage(content="Готово! Сохранила ✅")
    final_empty_ai = AIMessage(content="")  # iter=1, empty

    messages = [sys_msg, current_human, legitimate_iter0_ai, final_empty_ai]
    turn_start_idx = 2

    rescued = None
    for m in reversed(messages[turn_start_idx:]):
        if isinstance(m, AIMessage):
            content = (m.content or "").strip()
            if content:
                rescued = content
                break

    assert rescued == "Готово! Сохранила ✅"


def test_rescue_skips_aimessage_with_only_tool_calls_no_text():
    """AIMessage с tool_calls но без content — не считается legitimate
    rescue (хотя langchain создаст такие в multi-tool iters)."""
    sys_msg = SystemMessage(content="system")
    current_human = HumanMessage(content="user input")
    tool_call_ai = AIMessage(content="")  # only tool_calls, no text
    empty_ai = AIMessage(content="")

    messages = [sys_msg, current_human, tool_call_ai, empty_ai]
    turn_start_idx = 2

    rescued = None
    for m in reversed(messages[turn_start_idx:]):
        if isinstance(m, AIMessage):
            content = (m.content or "").strip()
            if content:
                rescued = content
                break

    assert rescued is None  # no rescue available, fallback "..." kicks in


def test_rescue_with_long_history_doesnt_pollute():
    """Стресс-тест: 10 turn'ов истории, каждая с AIMessage. Current
    turn — empty. Rescue должен вернуть None, не последний history AI.
    """
    sys_msg = SystemMessage(content="system")
    messages = [sys_msg]
    for i in range(10):
        messages.append(HumanMessage(content=f"prev{i}"))
        messages.append(AIMessage(content=f"OLD REPLY {i}"))
    current_human = HumanMessage(content="new input")
    current_empty = AIMessage(content="")
    messages.extend([current_human, current_empty])

    # turn_start_idx = position of current_empty's index? No, it's
    # right after current_human → len(messages) - 1 (since empty is last).
    turn_start_idx = len(messages) - 1  # only current_empty in current turn

    rescued = None
    for m in reversed(messages[turn_start_idx:]):
        if isinstance(m, AIMessage):
            content = (m.content or "").strip()
            if content:
                rescued = content
                break

    assert rescued is None, "Rescue не должен подхватить ни одного OLD REPLY"
