"""R-39 Slice 2: тесты adapter layer для housewife tools."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sreda.agents.r39_tool_adapter import (
    R39ToolFailure,
    is_past_iso,
    parse_ok_segment,
    parse_tool_result_or_raise,
)


# ─── R39ToolFailure exception ────────────────────────────────────────


def test_failure_exception_carries_attributes() -> None:
    exc = R39ToolFailure(
        error_code="past_date",
        error_message="trigger in past",
        raw="skipped:past:2020-01-01T00:00:00Z:late_by_1000000min",
    )
    assert exc.error_code == "past_date"
    assert exc.error_message == "trigger in past"
    assert "past" in exc.raw


# ─── Happy path: ok cases ────────────────────────────────────────────


def test_schedule_reminder_ok() -> None:
    raw = "ok:scheduled:rem_42:2026-05-17T14:00:00+03:00"
    result = parse_tool_result_or_raise("schedule_reminder", raw)
    assert result["status_token"] == "scheduled"
    assert result["entity_id"] == "rem_42"
    assert result["trigger_iso"] == "2026-05-17T14:00:00+03:00"
    assert result["raw_ok"] == raw


def test_update_reminder_ok() -> None:
    raw = "ok:updated:rem_99:2026-05-17T15:00:00+03:00"
    result = parse_tool_result_or_raise("update_reminder", raw)
    assert result["entity_id"] == "rem_99"
    assert result["trigger_iso"] == "2026-05-17T15:00:00+03:00"


def test_cancel_reminder_ok() -> None:
    raw = "ok:cancelled"
    result = parse_tool_result_or_raise("cancel_reminder", raw)
    assert result["raw_ok"] == raw
    # entity_id не в result — caller знает из args


def test_save_recipe_ok_saved() -> None:
    raw = "ok:saved:rec_123"
    result = parse_tool_result_or_raise("save_recipe", raw)
    assert result["status_token"] == "saved"
    assert result["entity_id"] == "rec_123"


def test_save_recipe_ok_duplicate() -> None:
    raw = "ok:duplicate:rec_456"
    result = parse_tool_result_or_raise("save_recipe", raw)
    assert result["status_token"] == "duplicate"
    assert result["entity_id"] == "rec_456"


def test_add_shopping_items_count_not_entity_id() -> None:
    """Codex R3 MAJ: ok:added:N:ids=[...] — N это count, НЕ entity_id."""
    raw = "ok:added:3:ids=[i1,i2,i3]"
    result = parse_tool_result_or_raise("add_shopping_items", raw)
    assert result["items_added_count"] == 3
    # entity_id явно НЕ должен быть установлен
    assert "entity_id" not in result


def test_complete_task_ok() -> None:
    raw = "ok:completed:tsk_55"
    result = parse_tool_result_or_raise("complete_task", raw)
    assert result["entity_id"] == "tsk_55"
    assert result["status_token"] == "completed"


def test_unknown_tool_ok_default() -> None:
    """Незарегистрированный tool — отдаём raw_ok без разбора."""
    raw = "ok:something_unknown"
    result = parse_tool_result_or_raise("unknown_tool", raw)
    assert result["raw_ok"] == raw


# ─── Error cases: → R39ToolFailure ──────────────────────────────────


def test_error_no_user_id() -> None:
    raw = "error: no user_id context"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("save_recipe", raw)
    assert exc_info.value.error_code == "no_user_id"


def test_error_entity_not_found() -> None:
    raw = "error: reminder 'rem_xxx' not found"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("cancel_reminder", raw)
    assert exc_info.value.error_code == "entity_not_found"


def test_error_parse_failure() -> None:
    raw = "error: cannot parse trigger_iso='через 2 часа'"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("schedule_reminder", raw)
    assert exc_info.value.error_code == "parse_failure"


def test_error_internal() -> None:
    raw = "error: internal"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("schedule_reminder", raw)
    assert exc_info.value.error_code == "tool_internal"


def test_error_empty_input() -> None:
    raw = "error: empty items list"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("add_shopping_items", raw)
    assert exc_info.value.error_code == "empty_input"


def test_error_generic_fallback() -> None:
    """Незнакомый текст ошибки → fallback на generic tool_error."""
    raw = "error: something weird happened"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("schedule_reminder", raw)
    assert exc_info.value.error_code == "tool_error"


# ─── Skipped cases ──────────────────────────────────────────────────


def test_skipped_past_yields_past_date_code() -> None:
    """Главный сценарий: schedule на past time → специальная фраза."""
    raw = "skipped:past:2020-01-01T00:00:00Z:late_by_3000000min"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("schedule_reminder", raw)
    assert exc_info.value.error_code == "past_date"
    assert "2020" in exc_info.value.error_message


def test_skipped_other() -> None:
    raw = "skipped:rate_limit"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("schedule_reminder", raw)
    assert exc_info.value.error_code == "skipped_other"


# ─── Edge cases ──────────────────────────────────────────────────────


def test_none_result_raises() -> None:
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("schedule_reminder", None)
    assert exc_info.value.error_code == "empty_result"


def test_unexpected_format_raises() -> None:
    raw = "wat???"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("schedule_reminder", raw)
    assert exc_info.value.error_code == "unexpected_format"


def test_non_string_raw_passes_through() -> None:
    """Для совместимости с моками возвращающими dict напрямую."""
    raw = {"reminder_id": "rem_x"}
    result = parse_tool_result_or_raise("schedule_reminder", raw)
    assert result["raw"] == raw


# ─── parse_ok_segment direct calls ───────────────────────────────────


def test_parse_ok_segment_schedule_short_format() -> None:
    """ok:scheduled без полного iso — return без trigger_iso."""
    raw = "ok:scheduled:rem_42"
    result = parse_ok_segment("schedule_reminder", raw)
    # Без 4-го segment — entity_id и trigger_iso не set'нем
    assert result["raw_ok"] == raw


def test_parse_ok_segment_add_shopping_bad_count() -> None:
    """Если N не int — items_added_count = 0 (graceful)."""
    raw = "ok:added:abc:ids=[]"
    result = parse_ok_segment("add_shopping_items", raw)
    assert result["items_added_count"] == 0


# ─── is_past_iso ─────────────────────────────────────────────────────


def test_is_past_iso_true_for_year_2020() -> None:
    assert is_past_iso("2020-01-01T00:00:00+00:00") is True


def test_is_past_iso_false_for_future() -> None:
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert is_past_iso(future) is False


def test_is_past_iso_grace_window_passes() -> None:
    """В пределах grace окна — возвращаем False (не past)."""
    # 30 секунд назад — внутри 2-min grace → False
    just_now = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    assert is_past_iso(just_now, grace_minutes=2) is False


def test_is_past_iso_outside_grace_is_past() -> None:
    """5 минут назад с grace=2 — past."""
    ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert is_past_iso(ago, grace_minutes=2) is True


def test_is_past_iso_unparseable_returns_false() -> None:
    """Graceful: не парсится → False, не сбоим adapter."""
    assert is_past_iso("через 2 часа") is False
    assert is_past_iso("") is False
    assert is_past_iso("not-an-iso") is False


def test_is_past_iso_naive_treated_as_utc() -> None:
    """Naive ISO (без offset) — считаем UTC."""
    # 1 hour ago UTC, без tzinfo
    past_naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert is_past_iso(past_naive) is True


def test_is_past_iso_z_format() -> None:
    """Z (Zulu/UTC) формат — поддерживается."""
    assert is_past_iso("2020-01-01T00:00:00Z") is True


# ─── Kati regression ────────────────────────────────────────────────


def test_kati_scenario_update_to_past_time_blocked() -> None:
    """LLM эмитит update_reminder(trigger_iso=<past>) → past_date.

    update_reminder в реальном housewife service не имеет past-date guard
    как schedule_reminder. Adapter ловит это preflight'ом.
    """
    past = "2020-05-17T14:00:00+03:00"
    assert is_past_iso(past) is True

    # При вызове через adapter (Slice 3) — wrapper raises R39ToolFailure
    # с error_code=past_date ДО tool.invoke. Тут проверяем сам _parse —
    # для случая когда tool успел вернуть skipped:past:
    raw = "skipped:past:2020-05-17T14:00:00+03:00:late_by_999999min"
    with pytest.raises(R39ToolFailure) as exc_info:
        parse_tool_result_or_raise("schedule_reminder", raw)
    assert exc_info.value.error_code == "past_date"
