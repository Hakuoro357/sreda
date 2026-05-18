"""R-39 Codex CRITICAL: тесты атомарной замены напоминания."""

from __future__ import annotations

import pytest

from sreda.agents.replace_reminder import (
    ReplaceOutcome,
    ReplaceResult,
    atomic_replace_reminder,
)


# ─── Фабрики помощников ───────────────────────────────────────────────


def _ok_cancel(reminder_id: str) -> None:
    return None


def _ok_create(title: str, trigger_iso: str) -> dict:
    return {"reminder_id": "rem_new", "title": title, "trigger_iso": trigger_iso}


def _failing_cancel(_: str) -> None:
    raise RuntimeError("cancel failed")


def _failing_create(_t: str, _i: str) -> None:
    raise RuntimeError("create failed")


# ─── Happy path ──────────────────────────────────────────────────────


def test_success_when_both_ok() -> None:
    result = atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="Разбудить Катю",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=_ok_cancel,
        create_fn=_ok_create,
    )
    assert result.outcome is ReplaceOutcome.SUCCESS
    assert result.cancelled_reminder_id == "rem_old"
    assert result.new_reminder_id == "rem_new"


# ─── Cancel сразу упал → нет изменений в БД ──────────────────────────


def test_total_failure_when_cancel_fails() -> None:
    result = atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="X",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=_failing_cancel,
        create_fn=_ok_create,
    )
    assert result.outcome is ReplaceOutcome.TOTAL_FAILURE
    assert result.cancelled_reminder_id is None
    assert result.new_reminder_id is None
    assert "cancel_failed" in (result.error or "")


def test_create_not_called_when_cancel_fails() -> None:
    calls: list[str] = []

    def cancel(r: str) -> None:
        calls.append(f"cancel:{r}")
        raise RuntimeError("nope")

    def create(t: str, i: str) -> dict:
        calls.append("create")
        return {"reminder_id": "x"}

    atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="X",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=cancel,
        create_fn=create,
    )
    assert "cancel:rem_old" in calls
    assert "create" not in calls


# ─── Create упал, есть rollback ──────────────────────────────────────


def test_rollback_called_when_create_fails() -> None:
    calls: list[str] = []

    def cancel(r: str) -> None:
        calls.append(f"cancel:{r}")

    def create(t: str, i: str) -> None:
        calls.append("create")
        raise RuntimeError("create failed")

    def rollback(r: str) -> None:
        calls.append(f"rollback:{r}")

    result = atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="X",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=cancel,
        create_fn=create,
        rollback_cancel_fn=rollback,
    )
    assert result.outcome is ReplaceOutcome.PARTIAL_ONLY_CANCELED
    assert result.rolled_back is True
    assert result.cancelled_reminder_id == "rem_old"
    assert result.new_reminder_id is None
    assert "rollback:rem_old" in calls
    assert "create_failed_rolled_back" in (result.error or "")


# ─── Create упал, нет rollback — data loss state ─────────────────────


def test_partial_when_create_fails_without_rollback() -> None:
    result = atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="X",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=_ok_cancel,
        create_fn=_failing_create,
        rollback_cancel_fn=None,
    )
    assert result.outcome is ReplaceOutcome.PARTIAL_ONLY_CANCELED
    assert result.rolled_back is False
    assert result.cancelled_reminder_id == "rem_old"
    assert result.new_reminder_id is None
    assert "create_failed_no_rollback" in (result.error or "")


# ─── Worst case: create упал + rollback тоже упал ────────────────────


def test_inconsistent_when_both_create_and_rollback_fail() -> None:
    def rollback_fails(_: str) -> None:
        raise RuntimeError("rollback failed too")

    result = atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="X",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=_ok_cancel,
        create_fn=_failing_create,
        rollback_cancel_fn=rollback_fails,
    )
    assert result.outcome is ReplaceOutcome.PARTIAL_INCONSISTENT
    assert result.rolled_back is False
    assert "create_failed_and_rollback_failed" in (result.error or "")


# ─── Извлечение new_id ───────────────────────────────────────────────


def test_extract_new_id_from_dict_id_field() -> None:
    """Поддержка id вместо reminder_id."""
    result = atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="X",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=_ok_cancel,
        create_fn=lambda t, i: {"id": "alt_id"},
    )
    assert result.outcome is ReplaceOutcome.SUCCESS
    assert result.new_reminder_id == "alt_id"


def test_custom_extract_new_id() -> None:
    """Кастомный extract_new_id для нетипичных результатов."""
    result = atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="X",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=_ok_cancel,
        create_fn=lambda t, i: ("custom_id_123",),
        extract_new_id=lambda r: r[0],
    )
    assert result.new_reminder_id == "custom_id_123"


def test_object_with_reminder_id_attribute() -> None:
    """Result является ORM-подобным объектом."""
    class FakeReminder:
        reminder_id = "obj_123"
        title = "X"

    result = atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="X",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=_ok_cancel,
        create_fn=lambda t, i: FakeReminder(),
    )
    assert result.new_reminder_id == "obj_123"


# ─── Кати-сценарий (главный регрессионный) ──────────────────────────


def test_kati_correction_happy_path() -> None:
    """Полный сценарий: 2 часа → 14:00, оба шага OK."""
    calls: list[str] = []

    def cancel(r: str) -> None:
        calls.append(f"cancel:{r}")

    def create(t: str, i: str) -> dict:
        calls.append(f"create:{t}:{i}")
        return {"reminder_id": "rem_new_kati"}

    result = atomic_replace_reminder(
        old_reminder_id="rem_old_02_00",
        new_title="Разбудить Катю",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=cancel,
        create_fn=create,
    )
    assert result.outcome is ReplaceOutcome.SUCCESS
    assert result.new_reminder_id == "rem_new_kati"
    assert result.cancelled_reminder_id == "rem_old_02_00"
    # Порядок гарантирован: cancel перед create
    assert calls == [
        "cancel:rem_old_02_00",
        "create:Разбудить Катю:2026-05-17T14:00:00+03:00",
    ]


def test_kati_correction_partial_when_create_fails() -> None:
    """Catastrophic: cancel прошёл, create упал, rollback есть → восстановили старое."""
    rolled_back: list[str] = []

    def rollback(r: str) -> None:
        rolled_back.append(r)

    result = atomic_replace_reminder(
        old_reminder_id="rem_old",
        new_title="Разбудить Катю",
        new_trigger_iso="2026-05-17T14:00:00+03:00",
        cancel_fn=_ok_cancel,
        create_fn=_failing_create,
        rollback_cancel_fn=rollback,
    )
    assert result.outcome is ReplaceOutcome.PARTIAL_ONLY_CANCELED
    assert result.rolled_back is True
    assert rolled_back == ["rem_old"]
