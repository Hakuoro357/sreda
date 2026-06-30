"""#269: outcome хода считается по ДЕЛЬТЕ (calls/messages этого хода), не по накопителю треда.

Регрессия: на durable-треде llm_calls/messages — add-reducer (копятся между ходами). Старый
fallback_fired / tool-error в накопителе навсегда красил каждый последующий ход → ложный #258-алерт.
Фикс: _turn_outcome берёт срез [prev_n:] (добавленное ИМЕННО этим ходом). tool_error детектится по
ToolMessage'ам дельты НАПРЯМУЮ (robust к resume — R2 Codex medium MAJOR).
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime import react_loop as rl


def _tool(error: bool, cid: str = "c1") -> ToolMessage:
    return ToolMessage(content="ok", tool_call_id=cid,
                       artifact={"result_kind": "error" if error else "ok"})


# --- fallback (по llm_calls-дельте) ---

def test_old_fallback_in_accumulator_clean_turn_is_ok():
    # накопитель: старый fallback (idx0) + чистый вызов этого хода (idx1); prev_lcs_n=1 → дельта=[clean]
    lcs = [{"fallback_fired": True}, {"fallback_fired": False}]
    oc, lcs_turn, _ = rl._turn_outcome(lcs, [], 1, 0, tenant_id="t")
    assert oc == "ok"                       # старый fallback НЕ красит чистый ход (был баг fallback_used)
    assert lcs_turn == [{"fallback_fired": False}]  # в трейс — дельта, не накопитель


def test_fallback_this_turn_is_fallback_used():
    lcs = [{"fallback_fired": False}, {"fallback_fired": True}]
    oc, _, _ = rl._turn_outcome(lcs, [], 1, 0, tenant_id="t")
    assert oc == "fallback_used"            # fallback ИМЕННО на этом ходу → алертим (анти-регресс #258)


# --- tool_error (по ToolMessage-дельте напрямую) ---

def test_tool_error_this_turn():
    # дельта сообщений (idx>=1) = [error ToolMessage] → tool_error
    msgs = [HumanMessage("q"), _tool(error=True)]
    oc, _, _ = rl._turn_outcome([], msgs, 0, 1, tenant_id="t")
    assert oc == "tool_error"


def test_resume_tool_error_detected_without_paired_aimessage():
    # R2 MAJOR: на resume AIMessage с tool_calls ДО паузы (вне дельты), ToolMessage(error) — в дельте.
    # collect_tool_calls по дельте вернул бы [] (нет пары) → раньше пропустили бы. Прямой детект ловит.
    msgs = [
        HumanMessage("удали рецепт"),
        AIMessage(content="", tool_calls=[{"name": "delete_recipe", "args": {}, "id": "c1"}]),  # до паузы
        _tool(error=True, cid="c1"),  # после resume — в дельте
    ]
    oc, _, _ = rl._turn_outcome([], msgs, 0, 2, tenant_id="t")  # prev_msgs_n=2 → дельта=[ToolMessage]
    assert oc == "tool_error"


def test_old_tool_error_excluded_from_delta():
    # старая ошибка в msgs[0] вне дельты (prev_msgs_n=1) → дельта чистая → ok
    msgs = [_tool(error=True), _tool(error=False)]
    oc, _, _ = rl._turn_outcome([], msgs, 0, 1, tenant_id="t")
    assert oc == "ok"


def test_tool_error_takes_priority_over_fallback():
    msgs = [HumanMessage("q"), _tool(error=True)]
    oc, _, _ = rl._turn_outcome([{"fallback_fired": True}], msgs, 0, 1, tenant_id="t")
    assert oc == "tool_error"


def test_empty_and_none_safe():
    oc, lcs_turn, tcs = rl._turn_outcome(None, [], 0, 0, tenant_id="t")
    assert oc == "ok" and lcs_turn == [] and tcs == []
