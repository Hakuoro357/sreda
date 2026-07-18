"""Tests for ``conversation_turns`` service helpers (Sub-A9, Epic #74).

Helpers live in ``sreda.services.conversation_turns``:

  open_turn(session, *, thread_id, tenant_id, now=None)
      → ConversationTurn. Closes any existing active turn for the
      thread before opening (per Group 6.6 — only one active per thread).

  close_turn(session, *, turn_id, thread_id, now=None)
      → bool. Conditional UPDATE; True if state was changed.

  get_active_turn(session, *, thread_id)
      → ConversationTurn | None.

  get_recent_closed_turns(session, *, thread_id, limit=5)
      → list[ConversationTurn] ordered by started_at DESC.

  should_hard_close(turn, *, now, hard_close_days=30)
      → bool. Pure function on a Turn snapshot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from sreda.db.models import AgentThread, ConversationTurn
from sreda.services.conversation_turns import (
    close_turn,
    get_active_turn,
    get_recent_closed_turns,
    open_turn,
    should_hard_close,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_thread(
    session: Session,
    *,
    thread_id: str = "thread_t1",
    tenant_id: str = "tenant_1",
) -> AgentThread:
    """Codex R1 MAJOR #3 — open_turn now derives tenant_id from the
    agent_thread row, so tests must seed a real thread before opening
    a turn. SQLite FK enforcement is off by default in the test
    fixture so we don't need parent tenant/workspace rows for the
    thread to insert.

    Audit 2026-07-18 (runtime-core #5 / FC-2): agent_threads теперь имеет
    UNIQUE (tenant_id, channel_type, external_chat_id), поэтому каждый
    seeded thread получает СВОЙ external_chat_id (= thread_id) — раньше
    фикстура сидила несколько threads с одной тройкой, что под новым
    констрейнтом невалидно (на смысл тестов turn-lifecycle chat_id не
    влияет)."""
    thread = AgentThread(
        id=thread_id,
        tenant_id=tenant_id,
        workspace_id="ws_test",
        channel_type="telegram",
        external_chat_id=thread_id,
        status="active",
    )
    session.add(thread)
    session.flush()
    return thread


@pytest.fixture(autouse=True)
def _seed_common_threads(db_session: Session):
    """Autouse — seed the common test thread_ids used across multiple
    tests so each test doesn't have to repeat the boilerplate. The
    ``thread_other`` / ``thread_a`` / ``thread_b`` variants get the
    same tenant ``t1`` to match callers below; ``thread_t1`` uses
    ``t1`` (NOT ``tenant_1`` — callers use the shorter form)."""
    for thread_id in ("thread_t1", "thread_other", "thread_a", "thread_b"):
        _seed_thread(db_session, thread_id=thread_id, tenant_id="t1")
    db_session.commit()
    yield


# ---------------------------------------------------------------------------
# open_turn
# ---------------------------------------------------------------------------


def test_open_turn_creates_active_with_seq_1(db_session: Session) -> None:
    t0 = _now()
    turn = open_turn(
        db_session,
        thread_id="thread_t1",
        tenant_id="t1",
        now=t0,
    )
    db_session.commit()

    assert turn.id.startswith("turn_")
    assert len(turn.id) > 5
    assert turn.thread_id == "thread_t1"
    assert turn.tenant_id == "t1"
    assert turn.status == "active"
    assert turn.turn_seq == 1
    # SQLite strips tz; production (Postgres) preserves it.
    started = (
        turn.started_at.replace(tzinfo=timezone.utc)
        if turn.started_at.tzinfo is None
        else turn.started_at
    )
    assert started == t0
    assert turn.closed_at is None


def test_open_turn_derives_tenant_from_thread(db_session: Session) -> None:
    """Codex R1 MAJOR #3 — when tenant_id is omitted, it's looked up
    from the AgentThread row (the source of truth)."""
    turn = open_turn(db_session, thread_id="thread_t1", now=_now())
    db_session.commit()
    assert turn.tenant_id == "t1"


def test_open_turn_rejects_tenant_mismatch(db_session: Session) -> None:
    """Codex R1 MAJOR #3 — supplying a tenant_id that doesn't match
    the thread's tenant raises ValueError synchronously."""
    with pytest.raises(ValueError, match="tenant_id mismatch"):
        open_turn(
            db_session,
            thread_id="thread_t1",
            tenant_id="wrong_tenant",
            now=_now(),
        )


def test_open_turn_rejects_unknown_thread(db_session: Session) -> None:
    """Codex R1 MAJOR #3 — opening against a non-existent thread fails
    loud, not with a quiet orphan turn."""
    with pytest.raises(ValueError, match="does not exist"):
        open_turn(
            db_session,
            thread_id="thread_does_not_exist",
            tenant_id="t1",
            now=_now(),
        )


def test_open_turn_increments_seq_per_thread(db_session: Session) -> None:
    """After opening + closing a few turns, seq for the same thread
    keeps incrementing. seq for a different thread starts fresh at 1."""
    t0 = _now()
    a = open_turn(db_session, thread_id="thread_a", tenant_id="t1", now=t0)
    db_session.commit()

    close_turn(
        db_session, turn_id=a.id, thread_id="thread_a", now=t0 + timedelta(seconds=1)
    )
    db_session.commit()

    b = open_turn(
        db_session, thread_id="thread_a", tenant_id="t1", now=t0 + timedelta(seconds=2)
    )
    db_session.commit()

    c = open_turn(
        db_session, thread_id="thread_other", tenant_id="t1", now=t0 + timedelta(seconds=3)
    )
    db_session.commit()

    assert b.turn_seq == 2, f"second turn in thread_a should be seq=2, got {b.turn_seq}"
    assert c.turn_seq == 1, f"first turn in thread_other should be seq=1, got {c.turn_seq}"


def test_open_turn_closes_existing_active(db_session: Session) -> None:
    """Group 6.6 — at most one active turn per thread. ``open_turn``
    auto-closes the previous active turn before opening a new one
    (defense in depth on top of the partial unique index)."""
    t0 = _now()
    first = open_turn(db_session, thread_id="thread_t1", tenant_id="t1", now=t0)
    db_session.commit()

    second = open_turn(
        db_session,
        thread_id="thread_t1",
        tenant_id="t1",
        now=t0 + timedelta(seconds=5),
    )
    db_session.commit()

    db_session.expire_all()
    reread_first = db_session.get(ConversationTurn, first.id)
    assert reread_first.status == "closed"
    assert reread_first.closed_at is not None
    assert second.status == "active"
    assert second.turn_seq == 2


# ---------------------------------------------------------------------------
# close_turn
# ---------------------------------------------------------------------------


def test_close_turn_transitions_active_to_closed(db_session: Session) -> None:
    t0 = _now()
    turn = open_turn(db_session, thread_id="thread_t1", tenant_id="t1", now=t0)
    db_session.commit()

    closed_at = t0 + timedelta(minutes=10)
    landed = close_turn(
        db_session, turn_id=turn.id, thread_id="thread_t1", now=closed_at
    )
    db_session.commit()

    assert landed is True
    db_session.expire_all()
    reread = db_session.get(ConversationTurn, turn.id)
    assert reread.status == "closed"


def test_close_turn_idempotent_returns_false_if_already_closed(
    db_session: Session,
) -> None:
    """Second close call on the same turn returns False — UPDATE
    matches the conditional WHERE only once."""
    t0 = _now()
    turn = open_turn(db_session, thread_id="thread_t1", tenant_id="t1", now=t0)
    db_session.commit()

    close_turn(db_session, turn_id=turn.id, thread_id="thread_t1", now=_now())
    db_session.commit()

    landed = close_turn(
        db_session, turn_id=turn.id, thread_id="thread_t1", now=_now()
    )
    assert landed is False


def test_close_turn_wrong_thread_returns_false(db_session: Session) -> None:
    """Safety: if the caller has the wrong thread_id, no-op. The
    composite-FK invariant on agent_runs guarantees thread_id is
    always known at close time."""
    t0 = _now()
    turn = open_turn(db_session, thread_id="thread_t1", tenant_id="t1", now=t0)
    db_session.commit()

    landed = close_turn(
        db_session, turn_id=turn.id, thread_id="WRONG", now=_now()
    )
    assert landed is False


# ---------------------------------------------------------------------------
# get_active_turn
# ---------------------------------------------------------------------------


def test_get_active_turn_returns_none_when_empty(db_session: Session) -> None:
    assert get_active_turn(db_session, thread_id="thread_t1") is None


def test_get_active_turn_returns_existing(db_session: Session) -> None:
    t0 = _now()
    turn = open_turn(db_session, thread_id="thread_t1", tenant_id="t1", now=t0)
    db_session.commit()

    found = get_active_turn(db_session, thread_id="thread_t1")
    assert found is not None
    assert found.id == turn.id


def test_get_active_turn_ignores_closed(db_session: Session) -> None:
    t0 = _now()
    turn = open_turn(db_session, thread_id="thread_t1", tenant_id="t1", now=t0)
    db_session.commit()
    close_turn(db_session, turn_id=turn.id, thread_id="thread_t1", now=_now())
    db_session.commit()

    assert get_active_turn(db_session, thread_id="thread_t1") is None


def test_get_active_turn_scoped_by_thread(db_session: Session) -> None:
    t0 = _now()
    open_turn(db_session, thread_id="thread_a", tenant_id="t1", now=t0)
    open_turn(db_session, thread_id="thread_b", tenant_id="t1", now=t0)
    db_session.commit()

    a = get_active_turn(db_session, thread_id="thread_a")
    b = get_active_turn(db_session, thread_id="thread_b")
    assert a is not None and b is not None
    assert a.thread_id == "thread_a"
    assert b.thread_id == "thread_b"
    assert a.id != b.id


# ---------------------------------------------------------------------------
# get_recent_closed_turns
# ---------------------------------------------------------------------------


def test_recent_closed_turns_ordering_and_limit(db_session: Session) -> None:
    t0 = _now()
    # Create 6 closed turns; helper should return 5 most-recent in
    # started_at-DESC order.
    for i in range(6):
        started = t0 + timedelta(minutes=i)
        turn = open_turn(
            db_session, thread_id="thread_t1", tenant_id="t1", now=started
        )
        db_session.commit()
        close_turn(
            db_session,
            turn_id=turn.id,
            thread_id="thread_t1",
            now=started + timedelta(seconds=10),
        )
        db_session.commit()

    rows = get_recent_closed_turns(db_session, thread_id="thread_t1", limit=5)
    assert len(rows) == 5
    # newest first
    seqs = [r.turn_seq for r in rows]
    assert seqs == sorted(seqs, reverse=True), f"expected DESC order, got {seqs}"


def test_recent_closed_turns_excludes_active(db_session: Session) -> None:
    t0 = _now()
    a = open_turn(db_session, thread_id="thread_t1", tenant_id="t1", now=t0)
    db_session.commit()
    close_turn(
        db_session, turn_id=a.id, thread_id="thread_t1", now=t0 + timedelta(seconds=1)
    )
    db_session.commit()

    # Open a fresh active turn after closing the first
    open_turn(
        db_session, thread_id="thread_t1", tenant_id="t1", now=t0 + timedelta(minutes=1)
    )
    db_session.commit()

    rows = get_recent_closed_turns(db_session, thread_id="thread_t1", limit=5)
    assert len(rows) == 1
    assert rows[0].id == a.id


# ---------------------------------------------------------------------------
# should_hard_close
# ---------------------------------------------------------------------------


def test_should_hard_close_true_for_old_active(db_session: Session) -> None:
    t0 = _now() - timedelta(days=45)
    turn = ConversationTurn(
        id="turn_old",
        turn_seq=1,
        thread_id="thread_t1",
        tenant_id="t1",
        started_at=t0,
        status="active",
        run_count=0,
        total_tokens=0,
        total_cost_usd=0,
    )
    assert (
        should_hard_close(turn, now=_now(), hard_close_days=30) is True
    )


def test_should_hard_close_false_for_fresh(db_session: Session) -> None:
    t0 = _now() - timedelta(hours=2)
    turn = ConversationTurn(
        id="turn_new",
        turn_seq=1,
        thread_id="thread_t1",
        tenant_id="t1",
        started_at=t0,
        status="active",
        run_count=0,
        total_tokens=0,
        total_cost_usd=0,
    )
    assert should_hard_close(turn, now=_now(), hard_close_days=30) is False


def test_should_hard_close_only_applies_to_active(db_session: Session) -> None:
    t0 = _now() - timedelta(days=400)
    turn = ConversationTurn(
        id="turn_ancient_closed",
        turn_seq=1,
        thread_id="thread_t1",
        tenant_id="t1",
        started_at=t0,
        closed_at=t0 + timedelta(hours=1),
        status="closed",
        run_count=0,
        total_tokens=0,
        total_cost_usd=0,
    )
    # closed turns don't need to be re-closed
    assert should_hard_close(turn, now=_now(), hard_close_days=30) is False
