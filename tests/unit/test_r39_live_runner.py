"""R-39 Slice 4: тесты live runner helpers + main flow.

Pure-logic тесты с моками — DB-зависимые (history loader, persist row,
correction_pending) идут в Slice 6 integration тесты.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from sreda.agents.contracts import (
    ResultKind,
    ToolJournalEntry,
)
from sreda.agents.r39_live_runner import (
    LiveResult,
    _deserialize_journal_entry,
    _parse_planner_json,
    _r39_admin_alert_adapter,
    _r39_result_data_extractor,
    _serialize_journal_entry,
    r39_try_live,
)


# ─── Mock helpers ────────────────────────────────────────────────────


class _StubRuntimeReply:
    """DI для RuntimeReply без зависимости от реального handlers."""
    def __init__(self, *, text, reply_markup, feature_key=None):
        self.text = text
        self.reply_markup = reply_markup
        self.feature_key = feature_key


class _StubFakeTool:
    def __init__(self, name: str, return_value: Any):
        self.name = name
        self.return_value = return_value
    def invoke(self, args):
        return self.return_value


# ─── _parse_planner_json ─────────────────────────────────────────────


def test_parse_planner_plain_json() -> None:
    result = _parse_planner_json('{"kind": "no_action", "ack": "Поняла"}')
    assert result == {"kind": "no_action", "ack": "Поняла"}


def test_parse_planner_fenced_json() -> None:
    """LLM gemini-3.1-flash-lite часто wrapping в ```json ... ```."""
    raw = '```json\n{"kind": "action", "calls": []}\n```'
    result = _parse_planner_json(raw)
    assert result == {"kind": "action", "calls": []}


def test_parse_planner_fenced_no_lang() -> None:
    raw = '```\n{"kind": "no_action"}\n```'
    result = _parse_planner_json(raw)
    assert result == {"kind": "no_action"}


def test_parse_planner_invalid_returns_none() -> None:
    assert _parse_planner_json("not json at all") is None


def test_parse_planner_empty_returns_none() -> None:
    assert _parse_planner_json("") is None
    assert _parse_planner_json("   ") is None


def test_parse_planner_array_returns_none() -> None:
    """Только dict accepted (не list, не scalar)."""
    assert _parse_planner_json('[1, 2, 3]') is None


# ─── _serialize / _deserialize_journal_entry round-trip ─────────────


def test_journal_entry_serialize_round_trip() -> None:
    original = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": "X", "trigger_human": "сегодня в 14:00"},
        entity_id="rem_42",
        idempotency_key="key1",
        error_code=None,
    )
    serialized = _serialize_journal_entry(original)
    restored = _deserialize_journal_entry(serialized)
    assert restored.tool_name == "schedule_reminder"
    assert restored.entity_id == "rem_42"
    assert restored.result_kind is ResultKind.SUCCESS
    assert restored.result_data["title"] == "X"


def test_journal_entry_serialize_failure_with_error_code() -> None:
    original = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.FAILURE,
        result_data={"error_code": "past_date"},
        error_message="trigger_iso in past",
        error_code="past_date",
        idempotency_key="key2",
    )
    serialized = _serialize_journal_entry(original)
    restored = _deserialize_journal_entry(serialized)
    assert restored.error_code == "past_date"
    assert restored.result_kind is ResultKind.FAILURE


def test_journal_entry_serialize_drops_non_scalar_values() -> None:
    """result_data filters только примитивы — list/dict dropped (не serializable JSON-stable)."""
    entry = ToolJournalEntry(
        tool_name="x",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"good": "str", "bad_list": [1, 2], "bad_dict": {"a": 1}, "int_ok": 42},
        idempotency_key="k",
    )
    serialized = _serialize_journal_entry(entry)
    assert serialized["result_data"]["good"] == "str"
    assert serialized["result_data"]["int_ok"] == 42
    assert "bad_list" not in serialized["result_data"]
    assert "bad_dict" not in serialized["result_data"]


# ─── _r39_admin_alert_adapter ────────────────────────────────────────


def test_admin_alert_adapter_calls_with_severity_p1() -> None:
    calls: list[dict] = []
    def fake_send(*, severity, title, body, dedupe_key=None, extra_context=None):
        calls.append({
            "severity": severity, "title": title, "body": body,
            "dedupe_key": dedupe_key, "extra_context": extra_context,
        })

    adapter = _r39_admin_alert_adapter(
        tenant_id="42", run_id="run-1", send_admin_alert_fn=fake_send,
    )
    adapter("Some L4 alert")

    assert len(calls) == 1
    assert calls[0]["severity"] == "P1"
    assert "R-39" in calls[0]["title"]
    assert calls[0]["body"] == "Some L4 alert"
    assert calls[0]["extra_context"]["tenant"] == "42"
    assert calls[0]["extra_context"]["run_id"] == "run-1"
    assert calls[0]["dedupe_key"].startswith("r39:run-1:")


def test_admin_alert_adapter_swallows_exceptions() -> None:
    """Adapter не должен propagate exception в caller."""
    def bad_send(**kwargs):
        raise RuntimeError("alerting broken")

    adapter = _r39_admin_alert_adapter(
        tenant_id="42", run_id="run-1", send_admin_alert_fn=bad_send,
    )
    # Не должно бросать
    adapter("text")


def test_admin_alert_adapter_truncates_long_body() -> None:
    captured: dict = {}
    def fake_send(*, severity, title, body, dedupe_key=None, extra_context=None):
        captured["body"] = body

    adapter = _r39_admin_alert_adapter(
        tenant_id="42", run_id="run-1", send_admin_alert_fn=fake_send,
    )
    long_text = "x" * 5000
    adapter(long_text)
    assert len(captured["body"]) <= 3900


# ─── _r39_result_data_extractor ──────────────────────────────────────


def test_extractor_adds_trigger_human_from_iso() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    args = {"title": "X", "trigger_iso": now_iso}
    raw_result = {"entity_id": "rem_X", "trigger_iso": now_iso}
    result = _r39_result_data_extractor(
        "schedule_reminder", args, raw_result, user_tz="Europe/Moscow",
    )
    assert "trigger_human" in result
    assert result["entity_id"] == "rem_X"
    assert result["title"] == "X"


def test_extractor_no_iso_no_trigger_human() -> None:
    args = {"title": "X"}
    raw_result = {"entity_id": "rem_X"}
    result = _r39_result_data_extractor(
        "save_recipe", args, raw_result, user_tz="Europe/Moscow",
    )
    assert "trigger_human" not in result


def test_extractor_unparseable_iso_uses_fallback() -> None:
    args = {"title": "X", "trigger_iso": "not-iso"}
    raw_result = {"trigger_iso": "not-iso"}
    result = _r39_result_data_extractor(
        "schedule_reminder", args, raw_result, user_tz="Europe/Moscow",
    )
    assert result["trigger_human"] == "(время не разобрано)"


def test_extractor_args_and_raw_both_merged() -> None:
    """raw_result имеет приоритет над args (более конкретный)."""
    args = {"title": "old", "trigger_iso": "old-iso"}
    raw_result = {"title": "new", "entity_id": "rem_X"}
    result = _r39_result_data_extractor(
        "schedule_reminder", args, raw_result, user_tz="Europe/Moscow",
    )
    assert result["title"] == "new"  # raw побеждает
    assert result["entity_id"] == "rem_X"


# ─── r39_try_live: смоук с моками ────────────────────────────────────


def test_try_live_preflight_crash_proceeded_false() -> None:
    """Pre-flight crash → fallback в legacy (proceeded=False)."""
    def bad_send(*args, **kwargs):
        pass

    # tools_list содержит объекты без .name — by_name = {}
    # build_r39_tool_callables не упадёт, но...
    # Хотим проверить crash в preflight — упадёт detect_unbacked_claim import?
    # detect_unbacked_claim существует — не упадёт.
    # Реальный pre-flight crash случится если session — None
    result = r39_try_live(
        session=None,  # type: ignore[arg-type] — намеренно ломаем pre-flight
        tenant_id="42",
        user_id="user1",
        user_text="привет",
        feature_key="housewife_assistant",
        run_id="run-1",
        user_tz="Europe/Moscow",
        tools_list=[],
        runtime_reply_cls=_StubRuntimeReply,
        send_admin_alert_fn=bad_send,
    )
    assert isinstance(result, LiveResult)
    assert result.proceeded is False
    assert result.side_effects_count == 0


def test_live_result_dataclass_defaults() -> None:
    """LiveResult с минимумом полей."""
    r = LiveResult(proceeded=False)
    assert r.proceeded is False
    assert r.reply is None
    assert r.side_effects_count == 0
    assert r.journal is None


def test_live_result_with_reply() -> None:
    reply = _StubRuntimeReply(text="Готово", reply_markup=None, feature_key="x")
    r = LiveResult(proceeded=True, reply=reply, side_effects_count=2)
    assert r.proceeded is True
    assert r.reply.text == "Готово"
    assert r.side_effects_count == 2
