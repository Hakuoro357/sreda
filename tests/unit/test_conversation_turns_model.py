"""Tests for ``ConversationTurn`` model + table constraints (Sub-A9, Epic #74).

``conversation_turns`` is the per-thread tematic-turn entity that lives
*inside* an ``agent_thread``. The planner decides ``is_new_turn`` per
incoming message; the executor opens a new turn or continues the
active one accordingly. See plan Group 6.6 for the full semantics.

Constraints that are load-bearing for correctness:

- ``UNIQUE (thread_id, turn_seq)`` — human-readable sequential id within
  a thread; uniqueness prevents accidental duplicate inserts under race.
- ``UNIQUE (id, thread_id)`` — composite key needed for the FK from
  ``agent_runs(turn_id, thread_id)`` so a run cannot accidentally point
  at a turn in a different thread.
- ``CHECK status IN ('active','closed')`` — enum honesty.
- ``CHECK (active AND closed_at IS NULL) OR (closed AND closed_at IS NOT NULL)``
  — state-machine consistency.
- ``UNIQUE INDEX ON (thread_id) WHERE status='active'`` — only one
  active turn per thread; defense-in-depth on top of advisory lock
  and queue-FIFO serialization.

Pg-specific behaviour (partial-index pushdown) is also exercised on
SQLite — SQLAlchemy translates ``sqlite_where`` for partial indexes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models import ConversationTurn


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_turn(**overrides) -> ConversationTurn:
    base = dict(
        id="turn_test_001",
        turn_seq=1,
        thread_id="thread_t1_dm",
        tenant_id="tenant_1",
        started_at=_now(),
        status="active",
        run_count=0,
        total_tokens=0,
        total_cost_usd=0,
    )
    base.update(overrides)
    return ConversationTurn(**base)


# ---------------------------------------------------------------------------
# Basic insert / persistence
# ---------------------------------------------------------------------------


def test_active_turn_persists(db_session: Session) -> None:
    turn = _make_turn()
    db_session.add(turn)
    db_session.commit()

    fetched = db_session.get(ConversationTurn, "turn_test_001")
    assert fetched is not None
    assert fetched.thread_id == "thread_t1_dm"
    assert fetched.turn_seq == 1
    assert fetched.status == "active"
    assert fetched.closed_at is None
    assert fetched.run_count == 0
    assert fetched.total_tokens == 0
    # total_cost_usd is Numeric — compare numerically, not by Decimal-vs-int identity
    assert float(fetched.total_cost_usd) == 0.0


def test_closed_turn_persists(db_session: Session) -> None:
    started = _now()
    closed = started + timedelta(minutes=10)
    turn = _make_turn(
        id="turn_closed_001",
        status="closed",
        started_at=started,
        closed_at=closed,
        summary="user asked about milk; added to shopping",
        run_count=3,
        total_tokens=12345,
        total_cost_usd=0.0042,
    )
    db_session.add(turn)
    db_session.commit()

    fetched = db_session.get(ConversationTurn, "turn_closed_001")
    assert fetched.status == "closed"
    # SQLite strips tzinfo from DateTime(timezone=True) columns — compare
    # the wall-clock value instead of identity. Postgres preserves tz;
    # production behaviour is correct, just the test backend doesn't.
    fetched_closed = (
        fetched.closed_at.replace(tzinfo=timezone.utc)
        if fetched.closed_at.tzinfo is None
        else fetched.closed_at
    )
    assert fetched_closed == closed
    assert fetched.summary == "user asked about milk; added to shopping"
    assert fetched.run_count == 3
    assert fetched.total_tokens == 12345
    assert float(fetched.total_cost_usd) == pytest.approx(0.0042)


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


def test_invalid_status_rejected(db_session: Session) -> None:
    turn = _make_turn(status="weird")
    db_session.add(turn)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_active_with_closed_at_rejected(db_session: Session) -> None:
    """Status='active' MUST have closed_at IS NULL."""
    turn = _make_turn(status="active", closed_at=_now())
    db_session.add(turn)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_closed_without_closed_at_rejected(db_session: Session) -> None:
    """Status='closed' MUST have closed_at NOT NULL."""
    turn = _make_turn(status="closed", closed_at=None)
    db_session.add(turn)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------


def test_duplicate_thread_seq_rejected(db_session: Session) -> None:
    """Two turns with the same (thread_id, turn_seq) are forbidden."""
    db_session.add(_make_turn(id="turn_a", turn_seq=5))
    db_session.commit()

    db_session.add(_make_turn(id="turn_b", turn_seq=5, status="closed", closed_at=_now()))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_same_seq_different_thread_allowed(db_session: Session) -> None:
    """``turn_seq`` is per-thread — two threads can both have seq=1."""
    db_session.add(_make_turn(id="turn_a", thread_id="thread_dm", turn_seq=1))
    db_session.commit()

    db_session.add(
        _make_turn(id="turn_b", thread_id="thread_group", turn_seq=1)
    )
    # second active turn for thread_group — fine
    db_session.commit()

    rows = (
        db_session.query(ConversationTurn)
        .filter_by(turn_seq=1)
        .order_by(ConversationTurn.thread_id)
        .all()
    )
    assert len(rows) == 2
    assert {r.thread_id for r in rows} == {"thread_dm", "thread_group"}


def test_two_active_turns_same_thread_rejected(db_session: Session) -> None:
    """Partial unique index ``ix_one_active_turn_per_thread`` enforces
    at-most-one active turn per thread. This is per-thread (Group 6.6
    update — was per-tenant in earlier draft)."""
    db_session.add(_make_turn(id="turn_a", turn_seq=1, status="active"))
    db_session.commit()

    db_session.add(_make_turn(id="turn_b", turn_seq=2, status="active"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_active_and_closed_same_thread_allowed(db_session: Session) -> None:
    """One active + many closed turns per thread is the normal state."""
    db_session.add(
        _make_turn(
            id="turn_old",
            turn_seq=1,
            status="closed",
            closed_at=_now() - timedelta(hours=2),
        )
    )
    db_session.add(_make_turn(id="turn_new", turn_seq=2, status="active"))
    db_session.commit()

    rows = (
        db_session.query(ConversationTurn)
        .filter_by(thread_id="thread_t1_dm")
        .order_by(ConversationTurn.turn_seq)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].status == "closed"
    assert rows[1].status == "active"


def test_composite_unique_id_thread(db_session: Session) -> None:
    """``UNIQUE (id, thread_id)`` — needed so ``agent_runs(turn_id,
    thread_id)`` can FK against the composite. Since ``id`` is already
    PK and globally unique, this index is a no-op on uniqueness but
    must exist as a constraint for Postgres to accept the composite FK.
    Verified through schema introspection."""
    inspector = inspect(db_session.bind)
    uniques = inspector.get_unique_constraints("conversation_turns")
    cols_sets = [tuple(sorted(u["column_names"])) for u in uniques]
    # Group 6.6 — both composite uniques must exist
    assert ("thread_id", "turn_seq") in cols_sets, (
        f"missing UNIQUE (thread_id, turn_seq); got: {cols_sets}"
    )
    assert ("id", "thread_id") in cols_sets, (
        f"missing UNIQUE (id, thread_id) needed for composite FK; got: {cols_sets}"
    )


# ---------------------------------------------------------------------------
# Indexes (sanity)
# ---------------------------------------------------------------------------


def test_partial_active_index_present(db_session: Session) -> None:
    inspector = inspect(db_session.bind)
    indexes = inspector.get_indexes("conversation_turns")
    names = {ix["name"] for ix in indexes}
    assert "ix_one_active_turn_per_thread" in names, (
        f"missing partial unique index; indexes={names}"
    )


def test_recent_index_present(db_session: Session) -> None:
    inspector = inspect(db_session.bind)
    indexes = inspector.get_indexes("conversation_turns")
    names = {ix["name"] for ix in indexes}
    assert "ix_conversation_turns_recent" in names, (
        f"missing recency index; indexes={names}"
    )


# ---------------------------------------------------------------------------
# agent_runs.turn_id integration
# ---------------------------------------------------------------------------


def test_agent_runs_has_turn_id_column(db_session: Session) -> None:
    """``turn_id`` column on agent_runs is nullable (Group 6.6 backfill
    strategy — legacy rows stay NULL)."""
    inspector = inspect(db_session.bind)
    columns = {c["name"]: c for c in inspector.get_columns("agent_runs")}
    assert "turn_id" in columns, (
        f"missing agent_runs.turn_id; columns={sorted(columns)}"
    )
    assert columns["turn_id"]["nullable"] is True, (
        "agent_runs.turn_id must be nullable for legacy backfill"
    )
