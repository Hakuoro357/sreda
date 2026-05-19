"""R-39 Slice 3: тесты фабрики tool callables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from sreda.agents.r39_tool_adapter import R39ToolFailure
from sreda.agents.r39_tool_callables import (
    REQUIRED_TOOLS,
    build_r39_tool_callables,
)


# ─── Fake tool helper ────────────────────────────────────────────────


class _FakeTool:
    """Подобие LangChain Tool: .name + .invoke(args_dict).

    Records calls for inspection.
    """
    def __init__(self, name: str, return_value: Any):
        self.name = name
        self.return_value = return_value
        self.invoke_calls: list[dict] = []

    def invoke(self, args: dict) -> Any:
        self.invoke_calls.append(args)
        return self.return_value


def _build(*fakes: _FakeTool) -> tuple[dict, dict]:
    """Хелпер: создаёт side_effects_state и dict callables."""
    state: dict = {}
    callables = build_r39_tool_callables(list(fakes), state)
    return state, callables


# ─── REQUIRED_TOOLS / structure ──────────────────────────────────────


def test_required_tools_all_present_in_callables() -> None:
    """build возвращает все REQUIRED_TOOLS даже если в tools_list пусто."""
    state, callables = _build()
    for name in REQUIRED_TOOLS:
        assert name in callables


def test_required_tools_uses_update_not_replace() -> None:
    """R7 patch: update_reminder, не replace_reminder."""
    assert "update_reminder" in REQUIRED_TOOLS
    assert "replace_reminder" not in REQUIRED_TOOLS


# ─── Missing tool → tool_not_registered ──────────────────────────────


def test_missing_tool_raises_at_invoke() -> None:
    """Tool отсутствует в tools_list — wrapper raises только при вызове."""
    state, callables = _build()  # empty list
    with pytest.raises(R39ToolFailure) as exc_info:
        callables["schedule_reminder"](title="X", trigger_iso="2099-01-01T00:00:00+00:00")
    assert exc_info.value.error_code == "tool_not_registered"


def test_missing_tool_does_not_mark_started() -> None:
    """Если tool отсутствует, side_effects_state.started остаётся False."""
    state, callables = _build()
    try:
        callables["schedule_reminder"](title="X", trigger_iso="2099-01-01T00:00:00+00:00")
    except R39ToolFailure:
        pass
    assert state["started"] is False


# ─── Happy path: ok results ──────────────────────────────────────────


def test_schedule_reminder_ok() -> None:
    fake = _FakeTool(
        "schedule_reminder",
        "ok:scheduled:rem_42:2099-05-17T14:00:00+03:00",
    )
    state, callables = _build(fake)
    result = callables["schedule_reminder"](
        title="Test", trigger_iso="2099-05-17T14:00:00+03:00"
    )
    assert result["entity_id"] == "rem_42"
    assert state["started"] is True
    assert state["count"] == 1
    assert fake.invoke_calls == [
        {"title": "Test", "trigger_iso": "2099-05-17T14:00:00+03:00"}
    ]


def test_update_reminder_ok() -> None:
    fake = _FakeTool(
        "update_reminder",
        "ok:updated:rem_99:2099-05-17T15:00:00+03:00",
    )
    state, callables = _build(fake)
    result = callables["update_reminder"](
        reminder_id="rem_99", trigger_iso="2099-05-17T15:00:00+03:00"
    )
    assert result["entity_id"] == "rem_99"
    assert state["count"] == 1


def test_cancel_reminder_ok_no_trigger_iso() -> None:
    """cancel_reminder не имеет trigger_iso — past-date guard не должен мешать."""
    fake = _FakeTool("cancel_reminder", "ok:cancelled")
    state, callables = _build(fake)
    result = callables["cancel_reminder"](reminder_id="rem_x")
    assert result["raw_ok"] == "ok:cancelled"
    assert state["count"] == 1


def test_save_recipe_ok() -> None:
    fake = _FakeTool("save_recipe", "ok:saved:rec_5")
    state, callables = _build(fake)
    result = callables["save_recipe"](title="Борщ", ingredients=[])
    assert result["entity_id"] == "rec_5"
    assert result["status_token"] == "saved"


def test_add_shopping_items_ok_count_not_entity_id() -> None:
    fake = _FakeTool("add_shopping_items", "ok:added:3:ids=[a,b,c]")
    state, callables = _build(fake)
    result = callables["add_shopping_items"](items=[{"title": "x"}])
    assert result["items_added_count"] == 3
    assert "entity_id" not in result


def test_complete_task_ok() -> None:
    fake = _FakeTool("complete_task", "ok:completed:tsk_7")
    state, callables = _build(fake)
    result = callables["complete_task"](task_id="tsk_7")
    assert result["entity_id"] == "tsk_7"


# ─── Past-date preflight (Codex R7 MAJ) ──────────────────────────────


def test_schedule_with_past_iso_blocked_before_invoke() -> None:
    """Past iso для schedule_reminder → past_date, tool.invoke НЕ вызывается."""
    fake = _FakeTool("schedule_reminder", "ok:scheduled:rem_X:foo")
    state, callables = _build(fake)
    with pytest.raises(R39ToolFailure) as exc_info:
        callables["schedule_reminder"](
            title="X", trigger_iso="2020-01-01T00:00:00+00:00"
        )
    assert exc_info.value.error_code == "past_date"
    # Critical: real tool НЕ вызывался
    assert fake.invoke_calls == []


def test_update_with_past_iso_blocked_before_invoke() -> None:
    """Past iso для update_reminder → past_date.

    Это главная защита R7: update_reminder в housewife service НЕ имеет
    past-date guard сам по себе (только schedule_reminder проверяет).
    Adapter ловит preflight'ом.
    """
    fake = _FakeTool("update_reminder", "ok:updated:rem_X:foo")
    state, callables = _build(fake)
    with pytest.raises(R39ToolFailure) as exc_info:
        callables["update_reminder"](
            reminder_id="rem_X", trigger_iso="2020-01-01T00:00:00+00:00"
        )
    assert exc_info.value.error_code == "past_date"
    assert fake.invoke_calls == []


def test_schedule_recurring_with_past_anchor_kept() -> None:
    """Codex MAJOR R2 code-review: recurring reminders с past anchor НЕ должны
    блокироваться preflight'ом — RRULE сама находит next future occurrence.
    Mirror'ит service layer (housewife_chat_tools.py:247) логику."""
    fake = _FakeTool("schedule_reminder", "ok:scheduled:rem_X:Daily")
    state, callables = _build(fake)
    # past anchor + recurrence_rule → preflight ДОЛЖЕН skip past-date check
    result = callables["schedule_reminder"](
        title="Daily pills",
        trigger_iso="2020-01-01T09:00:00+00:00",  # явно в прошлом
        recurrence_rule="FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
    )
    # Real tool ДОЛЖЕН быть вызван — preflight not blocking
    assert len(fake.invoke_calls) == 1
    assert fake.invoke_calls[0]["recurrence_rule"] == "FREQ=DAILY;BYHOUR=6;BYMINUTE=0"
    assert result is not None


def test_update_recurring_with_past_anchor_kept() -> None:
    """Codex MINOR R3 code-review: positive coverage для update_reminder
    happy path. Past trigger + active recurrence_rule (без clear_recurrence)
    → preflight skip, real tool invoked. Symmetric к schedule_reminder тесту."""
    fake = _FakeTool("update_reminder", "ok:updated:rem_X:Daily")
    state, callables = _build(fake)
    result = callables["update_reminder"](
        reminder_id="rem_X",
        trigger_iso="2020-01-01T09:00:00+00:00",  # past anchor
        recurrence_rule="FREQ=DAILY;BYHOUR=6;BYMINUTE=0",  # активная
        # clear_recurrence absent — recurrence net остаётся
    )
    # Real tool invoked — preflight skip past-date check
    assert len(fake.invoke_calls) == 1
    assert fake.invoke_calls[0]["recurrence_rule"] == "FREQ=DAILY;BYHOUR=6;BYMINUTE=0"
    assert result is not None


def test_update_with_clear_recurrence_and_past_iso_blocked() -> None:
    """Codex MINOR R2 code-review: update_reminder с recurrence_rule +
    clear_recurrence=True → net effect = снятие recurrence → past one-shot
    reminder. Preflight ДОЛЖЕН блокировать (нельзя skip'ать по recurrence_rule
    одному — проверять также clear_recurrence)."""
    fake = _FakeTool("update_reminder", "ok:updated:rem_X:foo")
    state, callables = _build(fake)
    with pytest.raises(R39ToolFailure) as exc_info:
        callables["update_reminder"](
            reminder_id="rem_X",
            trigger_iso="2020-01-01T00:00:00+00:00",  # past
            recurrence_rule="FREQ=DAILY;BYHOUR=6",  # ignored из-за clear_recurrence
            clear_recurrence=True,  # net: становится one-shot past reminder
        )
    assert exc_info.value.error_code == "past_date"
    assert fake.invoke_calls == []  # real tool НЕ вызывался


def test_past_date_preflight_does_not_mark_started() -> None:
    """Critical: started=False после past-date preflight → fallback safe."""
    fake = _FakeTool("update_reminder", "ok:updated:rem_X:foo")
    state, callables = _build(fake)
    try:
        callables["update_reminder"](
            reminder_id="rem_X", trigger_iso="2020-01-01T00:00:00+00:00"
        )
    except R39ToolFailure:
        pass
    assert state["started"] is False
    assert state["count"] == 0


def test_schedule_with_future_iso_passes_preflight() -> None:
    """Future iso — invoke вызывается нормально."""
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    fake = _FakeTool("schedule_reminder", f"ok:scheduled:rem_X:{future}")
    state, callables = _build(fake)
    result = callables["schedule_reminder"](title="X", trigger_iso=future)
    assert result["entity_id"] == "rem_X"
    assert fake.invoke_calls  # invoke реально дернулся


def test_cancel_with_past_iso_not_applicable() -> None:
    """cancel/save_recipe/add_shopping_items/complete_task не проверяют trigger_iso."""
    # У cancel нет trigger_iso в args вообще — guard не срабатывает
    fake = _FakeTool("cancel_reminder", "ok:cancelled")
    state, callables = _build(fake)
    # Даже если caller случайно передал past trigger_iso — для cancel это noise
    result = callables["cancel_reminder"](
        reminder_id="rem_X",
        trigger_iso="2020-01-01T00:00:00+00:00",
    )
    assert result["raw_ok"] == "ok:cancelled"


# ─── side_effects_state ──────────────────────────────────────────────


def test_started_set_before_invoke() -> None:
    """Codex R6 CRIT: started=True ДО tool.invoke (race window)."""
    state_snapshot_at_invoke: dict = {}

    class _RecordingTool:
        name = "schedule_reminder"
        def invoke(self, args):
            # Запомнить состояние ровно в момент invoke
            state_snapshot_at_invoke.update(state)
            return "ok:scheduled:rem_X:2099-01-01T00:00:00Z"

    state: dict = {}
    callables = build_r39_tool_callables([_RecordingTool()], state)
    callables["schedule_reminder"](
        title="X", trigger_iso="2099-01-01T00:00:00+00:00"
    )
    # В момент invoke флаг уже True
    assert state_snapshot_at_invoke.get("started") is True


def test_count_not_incremented_on_failure() -> None:
    """error: result → R39ToolFailure → count НЕ увеличивается, started=True остаётся."""
    fake = _FakeTool("schedule_reminder", "error: internal")
    state, callables = _build(fake)
    with pytest.raises(R39ToolFailure):
        callables["schedule_reminder"](
            title="X", trigger_iso="2099-01-01T00:00:00+00:00"
        )
    # started ДО invoke set → True остаётся (показывает что tool пытался)
    assert state["started"] is True
    # Но count не инкрементировался (FAILURE)
    assert state["count"] == 0


def test_multiple_ok_calls_increment_count() -> None:
    fake1 = _FakeTool("cancel_reminder", "ok:cancelled")
    fake2 = _FakeTool("schedule_reminder", "ok:scheduled:rem_2:2099-01-01T00:00:00Z")
    state, callables = _build(fake1, fake2)
    callables["cancel_reminder"](reminder_id="rem_1")
    callables["schedule_reminder"](title="X", trigger_iso="2099-01-01T00:00:00+00:00")
    assert state["count"] == 2


# ─── Tool error result → R39ToolFailure ───────────────────────────────


def test_error_result_raises() -> None:
    fake = _FakeTool("schedule_reminder", "error: no user_id context")
    state, callables = _build(fake)
    with pytest.raises(R39ToolFailure) as exc_info:
        callables["schedule_reminder"](
            title="X", trigger_iso="2099-01-01T00:00:00+00:00"
        )
    assert exc_info.value.error_code == "no_user_id"


def test_skipped_past_from_tool_raises_past_date() -> None:
    """Если tool сам вернул skipped:past (schedule в прошлое до нашего preflight),
    parse_tool_result_or_raise всё равно raises past_date."""
    # Передадим future iso (preflight пропустит), но fake вернёт skipped:past:
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    fake = _FakeTool(
        "schedule_reminder",
        "skipped:past:2020-01-01T00:00:00+00:00:late_by_999min",
    )
    state, callables = _build(fake)
    with pytest.raises(R39ToolFailure) as exc_info:
        callables["schedule_reminder"](title="X", trigger_iso=future)
    assert exc_info.value.error_code == "past_date"
