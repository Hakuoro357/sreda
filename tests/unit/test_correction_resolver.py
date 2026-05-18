"""R-39: тесты correction_resolver.

Главный регрессионный сценарий — Кати-кейс 2026-05-17 13:21:

  Turn N-1: «Поставь напоминание на 2 часа разбудить Катю»
            → schedule_reminder SUCCESS, entity_id=rem_old
  Turn N:   «Нет, не на 2 а на 14:00 разбудить Катю»
            → correction_resolver должен вернуть ResolvedCorrection(rem_old)
"""

from __future__ import annotations

from sreda.agents.contracts import (
    ConversationTurn,
    ResultKind,
    ToolJournalEntry,
)
from sreda.agents.correction_resolver import (
    AmbiguousCorrection,
    NoCorrectionTarget,
    ResolvedCorrection,
    resolve_correction_target,
)


# ─── Фабрики ─────────────────────────────────────────────────────────


def _success_schedule(entity_id: str, title: str = "Разбудить") -> ToolJournalEntry:
    return ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": title, "trigger_human": "сегодня в 14:00"},
        entity_id=entity_id,
        idempotency_key=f"key-{entity_id}",
    )


def _success_cancel(entity_id: str) -> ToolJournalEntry:
    return ToolJournalEntry(
        tool_name="cancel_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"reminder_id": entity_id},
        entity_id=entity_id,
        idempotency_key=f"cancel-{entity_id}",
    )


def _success_replace(entity_id: str, title: str = "Перенесено") -> ToolJournalEntry:
    return ToolJournalEntry(
        tool_name="replace_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": title, "trigger_human": "завтра в 9:00"},
        entity_id=entity_id,
        idempotency_key=f"replace-{entity_id}",
    )


def _turn(turn_id: str, entries: list[ToolJournalEntry], user_text: str = "") -> ConversationTurn:
    return ConversationTurn(
        user_text=user_text,
        journal_entries=tuple(entries),
        turn_id=turn_id,
        timestamp_utc="2026-05-17T13:21:00Z",
    )


# ─── Базовое поведение ───────────────────────────────────────────────


def test_empty_history_returns_no_target() -> None:
    r = resolve_correction_target("нет, не так", [])
    assert isinstance(r, NoCorrectionTarget)
    assert r.reason == "empty_history"


def test_single_recent_schedule_resolves() -> None:
    """Самый частый случай: один schedule в lookback → target = rem_id."""
    history = [
        _turn("t-1", [_success_schedule("rem_42", "Разбудить Катю")]),
    ]
    r = resolve_correction_target("нет, не на 2 а на 14", history)
    assert isinstance(r, ResolvedCorrection)
    assert r.target_entity_id == "rem_42"
    assert r.target_title == "Разбудить Катю"
    assert r.target_tool == "schedule_reminder"
    assert r.source_turn_id == "t-1"


def test_replace_reminder_also_resolves() -> None:
    history = [
        _turn("t-1", [_success_replace("rem_55", "Поменялось")]),
    ]
    r = resolve_correction_target("нет, ещё раз поменяй", history)
    assert isinstance(r, ResolvedCorrection)
    assert r.target_entity_id == "rem_55"
    assert r.target_tool == "replace_reminder"


# ─── Исключение отменённых ───────────────────────────────────────────


def test_cancelled_reminder_excluded_from_candidates() -> None:
    """Если в окне истории cancel того же id — не считаем кандидатом."""
    history = [
        _turn("t-1", [_success_schedule("rem_42")]),
        _turn("t-2", [_success_cancel("rem_42")]),
    ]
    r = resolve_correction_target("нет, верни", history)
    assert isinstance(r, NoCorrectionTarget)


def test_active_reminder_resolved_even_if_another_cancelled() -> None:
    history = [
        _turn("t-1", [_success_schedule("rem_old")]),
        _turn("t-2", [_success_cancel("rem_old")]),
        _turn("t-3", [_success_schedule("rem_new", "Новое")]),
    ]
    r = resolve_correction_target("нет, поправь", history)
    assert isinstance(r, ResolvedCorrection)
    assert r.target_entity_id == "rem_new"


# ─── Несколько кандидатов → AmbiguousCorrection ──────────────────────


def test_multiple_active_reminders_yields_ambiguous() -> None:
    history = [
        _turn("t-1", [_success_schedule("rem_a", "Принять таблетки")]),
        _turn("t-2", [_success_schedule("rem_b", "Купить хлеб")]),
    ]
    r = resolve_correction_target("нет, не так", history)
    assert isinstance(r, AmbiguousCorrection)
    assert len(r.candidates) == 2
    # Свежее идёт первым (порядок reverse'нут)
    assert r.candidates[0].target_entity_id == "rem_b"
    assert r.candidates[1].target_entity_id == "rem_a"


# ─── lookback_turns ──────────────────────────────────────────────────


def test_lookback_excludes_older_turns() -> None:
    """Слишком старые ходы не считаются."""
    history = [
        _turn("t-old", [_success_schedule("rem_old")]),
        _turn("t-1", [], user_text="как дела"),
        _turn("t-2", [], user_text="спасибо"),
        _turn("t-3", [], user_text="продолжай"),
    ]
    # lookback=3 захватит только t-1, t-2, t-3 — без rem_old
    r = resolve_correction_target("нет, поправь", history, lookback_turns=3)
    assert isinstance(r, NoCorrectionTarget)


def test_lookback_includes_within_window() -> None:
    history = [
        _turn("t-1", [_success_schedule("rem_42")]),
        _turn("t-2", []),
        _turn("t-3", []),
    ]
    r = resolve_correction_target("нет, поправь", history, lookback_turns=3)
    assert isinstance(r, ResolvedCorrection)
    assert r.target_entity_id == "rem_42"


# ─── Failure entries не считаются ────────────────────────────────────


def test_failed_schedule_not_a_candidate() -> None:
    failed = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.FAILURE,
        result_data={},
        entity_id="rem_fail",
        idempotency_key="x",
        error_message="db_error",
    )
    history = [_turn("t-1", [failed])]
    r = resolve_correction_target("нет, поправь", history)
    assert isinstance(r, NoCorrectionTarget)


def test_entry_without_entity_id_skipped() -> None:
    """Запись без entity_id (например, не возвращена БД) не считается."""
    entry = ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.SUCCESS,
        result_data={"title": "X"},
        entity_id=None,
        idempotency_key="x",
    )
    history = [_turn("t-1", [entry])]
    r = resolve_correction_target("нет, поправь", history)
    assert isinstance(r, NoCorrectionTarget)


# ─── Кати-сценарий (главный регрессионный) ──────────────────────────


def test_kati_correction_full_history() -> None:
    """Полный Кати-сценарий 2026-05-17 13:21:

    Turn 1: пользователь говорит «поставь на 2 часа»
    Bot: schedule_reminder rem_old SUCCESS, title="Разбудить"
    Turn 2: пользователь говорит «нет, не на 2 а на 14:00 разбудить Катю»
    correction_resolver должен вернуть ResolvedCorrection(rem_old)
    → executor вызовет replace_reminder(rem_old, "Разбудить Катю", "...14:00")
    """
    history = [
        _turn(
            "t-original",
            [_success_schedule("rem_old", title="Разбудить")],
            user_text="поставь напоминание на 2 часа разбудить Катю",
        ),
    ]
    r = resolve_correction_target(
        "Нет, не на 2 а на 14:00 разбудить Катю",
        history,
    )
    assert isinstance(r, ResolvedCorrection)
    assert r.target_entity_id == "rem_old"
    assert r.target_title == "Разбудить"
    assert r.target_tool == "schedule_reminder"
    assert r.source_turn_id == "t-original"


def test_reschedule_after_cancel_resolves(  # noqa: D401
) -> None:
    """R-39 review CRITICAL: schedule → cancel → schedule того же id.

    Свежий re-schedule должен оживить кандидата (last-write-wins).
    Раньше cancelled_ids накапливался и навсегда исключал id.
    """
    history = [
        _turn("t-1", [_success_schedule("rem_A", "Первое")]),
        _turn("t-2", [_success_cancel("rem_A")]),
        _turn("t-3", [_success_schedule("rem_A", "Возрождённое")]),
    ]
    r = resolve_correction_target("нет, поправь", history)
    assert isinstance(r, ResolvedCorrection)
    assert r.target_entity_id == "rem_A"
    assert r.target_title == "Возрождённое"
    assert r.source_turn_id == "t-3"


def test_dedup_same_entity_across_turns() -> None:
    """Один и тот же entity_id в двух ходах (например replace в более старом
    ходу и schedule оригинала ещё раньше) дедупится — берём свежий."""
    history = [
        _turn("t-1", [_success_schedule("rem_42", "Старое")]),
        _turn("t-2", [_success_replace("rem_42", "Новое")]),
    ]
    r = resolve_correction_target("нет, поправь", history)
    assert isinstance(r, ResolvedCorrection)
    assert r.target_entity_id == "rem_42"
    # Свежий ход побеждает
    assert r.target_tool == "replace_reminder"
    assert r.target_title == "Новое"
