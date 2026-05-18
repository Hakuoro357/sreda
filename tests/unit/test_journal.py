"""R-39: тесты ToolJournal — журнала действий одного хода."""

from __future__ import annotations

from sreda.agents.contracts import ResultKind, ToolJournalEntry
from sreda.agents.journal import ToolJournal


# ─── Базовое поведение ────────────────────────────────────────────────


def _entry(
    name: str = "schedule_reminder",
    index: int = 0,
    kind: ResultKind = ResultKind.SUCCESS,
    key: str | None = None,
    data: dict | None = None,
) -> ToolJournalEntry:
    return ToolJournalEntry(
        tool_name=name,
        action_index=index,
        result_kind=kind,
        result_data=data or {},
        idempotency_key=key,
    )


def test_empty_journal_is_empty() -> None:
    j = ToolJournal()
    assert j.is_empty
    assert len(j) == 0
    assert list(j) == []


def test_append_adds_entry() -> None:
    j = ToolJournal()
    assert j.append(_entry()) is True
    assert len(j) == 1
    assert not j.is_empty


def test_iter_yields_entries_in_order() -> None:
    j = ToolJournal()
    j.append(_entry(index=0, key="k1"))
    j.append(_entry(index=1, key="k2"))
    j.append(_entry(index=2, key="k3"))
    assert [e.action_index for e in j] == [0, 1, 2]


# ─── Dedup по idempotency_key ─────────────────────────────────────────


def test_duplicate_key_not_appended() -> None:
    j = ToolJournal()
    assert j.append(_entry(key="abc")) is True
    assert j.append(_entry(key="abc")) is False  # дубликат
    assert len(j) == 1


def test_different_keys_both_appended() -> None:
    j = ToolJournal()
    j.append(_entry(key="abc"))
    j.append(_entry(key="def"))
    assert len(j) == 2


def test_empty_key_skips_dedup() -> None:
    """Записи без ключа добавляются всегда (read-tools, generic)."""
    j = ToolJournal()
    j.append(_entry(key=None))
    j.append(_entry(key=None))
    assert len(j) == 2


def test_has_key_check() -> None:
    j = ToolJournal()
    j.append(_entry(key="abc"))
    assert j.has_key("abc")
    assert not j.has_key("xyz")


def test_find_by_key_returns_entry() -> None:
    j = ToolJournal()
    e = _entry(key="abc", data={"title": "X"})
    j.append(e)
    found = j.find_by_key("abc")
    assert found is e


def test_find_by_key_returns_none_when_missing() -> None:
    j = ToolJournal()
    assert j.find_by_key("nope") is None


# ─── Полярность ───────────────────────────────────────────────────────


def test_has_failures_true_on_failure() -> None:
    j = ToolJournal()
    j.append(_entry(kind=ResultKind.SUCCESS, key="a"))
    j.append(_entry(kind=ResultKind.FAILURE, key="b"))
    assert j.has_failures


def test_has_failures_true_on_partial() -> None:
    j = ToolJournal()
    j.append(_entry(kind=ResultKind.PARTIAL, key="a"))
    assert j.has_failures


def test_all_succeeded_true_when_all_success() -> None:
    j = ToolJournal()
    j.append(_entry(kind=ResultKind.SUCCESS, key="a"))
    j.append(_entry(kind=ResultKind.SUCCESS, key="b"))
    assert j.all_succeeded


def test_all_succeeded_false_on_empty() -> None:
    """Пустой журнал — нечего считать успешным."""
    j = ToolJournal()
    assert not j.all_succeeded


def test_all_succeeded_false_on_any_failure() -> None:
    j = ToolJournal()
    j.append(_entry(kind=ResultKind.SUCCESS, key="a"))
    j.append(_entry(kind=ResultKind.FAILURE, key="b"))
    assert not j.all_succeeded


def test_successful_filter() -> None:
    j = ToolJournal()
    j.append(_entry(kind=ResultKind.SUCCESS, key="a"))
    j.append(_entry(kind=ResultKind.FAILURE, key="b"))
    j.append(_entry(kind=ResultKind.SUCCESS, key="c"))
    assert len(j.successful) == 2
    assert all(e.result_kind is ResultKind.SUCCESS for e in j.successful)


def test_failed_filter() -> None:
    j = ToolJournal()
    j.append(_entry(kind=ResultKind.SUCCESS, key="a"))
    j.append(_entry(kind=ResultKind.FAILURE, key="b"))
    assert len(j.failed) == 1
    assert j.failed[0].result_kind is ResultKind.FAILURE


# ─── overall_kind (review MAJOR 2) ────────────────────────────────────


def test_overall_kind_empty_is_failure() -> None:
    """Пустой журнал — для рендера эквивалент FAILURE (нечего рендерить успехом)."""
    from sreda.agents.contracts import ResultKind
    j = ToolJournal()
    assert j.overall_kind is ResultKind.FAILURE


def test_overall_kind_all_success_is_success() -> None:
    from sreda.agents.contracts import ResultKind
    j = ToolJournal()
    j.append(_entry(kind=ResultKind.SUCCESS, key="a"))
    j.append(_entry(kind=ResultKind.SUCCESS, key="b"))
    assert j.overall_kind is ResultKind.SUCCESS


def test_overall_kind_all_failure_is_failure() -> None:
    from sreda.agents.contracts import ResultKind
    j = ToolJournal()
    j.append(_entry(kind=ResultKind.FAILURE, key="a"))
    j.append(_entry(kind=ResultKind.FAILURE, key="b"))
    assert j.overall_kind is ResultKind.FAILURE


def test_overall_kind_mixed_is_partial() -> None:
    """Multi-step Кати-сценарий с fail-fast halt: SUCCESS+FAILURE → PARTIAL."""
    from sreda.agents.contracts import ResultKind
    j = ToolJournal()
    j.append(_entry(name="cancel_reminder", index=0, kind=ResultKind.SUCCESS, key="a"))
    j.append(_entry(name="schedule_reminder", index=1, kind=ResultKind.FAILURE, key="b"))
    assert j.overall_kind is ResultKind.PARTIAL


def test_overall_kind_partial_entry_promotes_to_partial() -> None:
    from sreda.agents.contracts import ResultKind
    j = ToolJournal()
    j.append(_entry(kind=ResultKind.PARTIAL, key="a"))
    assert j.overall_kind is ResultKind.PARTIAL


# ─── Кати-сценарий: cancel + schedule в одном журнале ─────────────────


def test_kati_two_entry_journal() -> None:
    j = ToolJournal()
    j.append(_entry(
        name="cancel_reminder",
        index=0,
        kind=ResultKind.SUCCESS,
        key="cancel-key",
        data={"title": "Разбудить"},
    ))
    j.append(_entry(
        name="schedule_reminder",
        index=1,
        kind=ResultKind.SUCCESS,
        key="schedule-key",
        data={"title": "Разбудить Катю", "trigger_human": "сегодня в 14:00"},
    ))
    assert len(j) == 2
    assert j.all_succeeded
    assert not j.has_failures
