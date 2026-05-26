"""Helpers for ``conversation_turns`` (Sub-A9 of Plan-Execute Epic #74).

Public surface used by the planner-flow Stage 2 (turn transition):

  open_turn               start a new active turn; auto-close any
                          existing active turn for the same thread.
  close_turn              conditional UPDATE to transition active→closed.
                          Returns ``True`` if state changed.
  get_active_turn         SELECT the (at most one) active turn for a
                          thread.
  get_recent_closed_turns SELECT last N closed turns for the prompt
                          builder (Category B short-history block).
  should_hard_close       pure function: is this turn too old to keep
                          active without explicit closure?

These helpers are intentionally synchronous + transaction-aware: the
caller controls commit so a turn transition is atomic with the
``planner_executions`` UPDATE on the same Stage 2 transaction (Group 6.6
+ Category B Stage 2).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from sreda.db.models import AgentThread, ConversationTurn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_turn_id() -> str:
    """Generate a globally-unique turn id.

    Format: ``turn_<24-char hex>``. UUID-based because the composite FK
    on ``agent_runs(turn_id, thread_id)`` needs the id token to be
    unique across threads (Codex hollistic CRITICAL on earlier
    per-thread-seq draft of id)."""
    return f"turn_{uuid.uuid4().hex[:24]}"


def _next_seq(session: Session, *, thread_id: str) -> int:
    """Next ``turn_seq`` for this thread = max(existing) + 1, starting at 1.

    We rely on the per-thread FIFO worker (Sub-A2 message queue) +
    advisory_xact_lock at Stage 2 (Category B) to serialize callers,
    so a non-locking MAX scan is sufficient. The UNIQUE constraint on
    (thread_id, turn_seq) is the at-rest safety net.
    """
    current_max = session.execute(
        select(func.coalesce(func.max(ConversationTurn.turn_seq), 0)).where(
            ConversationTurn.thread_id == thread_id
        )
    ).scalar()
    return int(current_max or 0) + 1


def open_turn(
    session: Session,
    *,
    thread_id: str,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> ConversationTurn:
    """Open a fresh active turn for ``thread_id``.

    If an active turn already exists for the thread, close it first
    (defense in depth on top of the partial unique index — the caller
    might legitimately not know about a pre-existing active turn after
    a process restart).

    Codex R1 MAJOR #3 — ``tenant_id`` is now derived from
    ``agent_threads.tenant_id`` to prevent a caller from accidentally
    binding a turn to the wrong tenant (think misattributed billing /
    summaries). If the caller supplies a ``tenant_id``, we cross-check
    it against the thread's tenant; mismatch raises ``ValueError``
    so the bug surfaces synchronously, not as a quiet data drift.
    """
    when = now or _now()

    thread = session.get(AgentThread, thread_id)
    if thread is None:
        raise ValueError(
            f"open_turn: agent_thread {thread_id!r} does not exist; "
            "create the thread before opening a conversation turn."
        )

    if tenant_id is None:
        tenant_id = thread.tenant_id
    elif tenant_id != thread.tenant_id:
        raise ValueError(
            f"open_turn: tenant_id mismatch — caller passed "
            f"{tenant_id!r}, but thread {thread_id!r} belongs to "
            f"{thread.tenant_id!r}. Refusing to create a misattributed "
            "turn."
        )

    # Close any pre-existing active turn (idempotent close).
    existing = get_active_turn(session, thread_id=thread_id)
    if existing is not None:
        close_turn(
            session,
            turn_id=existing.id,
            thread_id=thread_id,
            now=when,
        )

    turn = ConversationTurn(
        id=_new_turn_id(),
        turn_seq=_next_seq(session, thread_id=thread_id),
        thread_id=thread_id,
        tenant_id=tenant_id,
        started_at=when,
        status="active",
        run_count=0,
        total_tokens=0,
        total_cost_usd=0,
    )
    session.add(turn)
    session.flush()
    return turn


def close_turn(
    session: Session,
    *,
    turn_id: str,
    thread_id: str,
    now: datetime | None = None,
) -> bool:
    """Conditional UPDATE active → closed.

    Returns ``True`` if the row transitioned (state changed). False
    means the turn was already closed, doesn't exist, or the
    ``thread_id`` doesn't match — the caller can decide whether that's
    expected (idempotent retry) or a bug.

    Codex Sub-A9 R1 MINOR #7 — raw SQL doesn't synchronize the
    ORM identity map, so we explicitly expire the loaded instance
    (if any) after the UPDATE. Subsequent ``session.get(ConversationTurn,
    turn_id)`` reads the fresh state instead of stale ``status='active'``.
    """
    when = now or _now()
    result = session.execute(
        text(
            """
            UPDATE conversation_turns
            SET status = 'closed', closed_at = :now
            WHERE id = :id
              AND thread_id = :thread_id
              AND status = 'active'
            """
        ),
        {"id": turn_id, "thread_id": thread_id, "now": when},
    )
    if result.rowcount:
        # Refresh any loaded instance in the identity map so subsequent
        # reads in the same session see the new status / closed_at.
        loaded = session.get(ConversationTurn, turn_id)
        if loaded is not None:
            session.expire(loaded, ["status", "closed_at"])
        return True
    return False


def get_active_turn(
    session: Session, *, thread_id: str
) -> ConversationTurn | None:
    """Return the (at most one) active turn for ``thread_id``."""
    return session.execute(
        select(ConversationTurn).where(
            ConversationTurn.thread_id == thread_id,
            ConversationTurn.status == "active",
        )
    ).scalar_one_or_none()


def get_recent_closed_turns(
    session: Session,
    *,
    thread_id: str,
    limit: int = 5,
) -> list[ConversationTurn]:
    """Most recent ``limit`` closed turns for ``thread_id`` (newest first).

    Used by the prompt builder to surface the last few closed turns'
    summaries (Category B — short history block above the active turn)."""
    # Codex Sub-A9 R1 MINOR #8 — order by (started_at DESC, turn_seq
    # DESC) so equal timestamps (common in batched tests and possible
    # in prod under burst load) produce a deterministic order.
    rows = session.execute(
        select(ConversationTurn)
        .where(
            ConversationTurn.thread_id == thread_id,
            ConversationTurn.status == "closed",
        )
        .order_by(
            ConversationTurn.started_at.desc(),
            ConversationTurn.turn_seq.desc(),
        )
        .limit(limit)
    ).scalars().all()
    return list(rows)


def should_hard_close(
    turn: ConversationTurn,
    *,
    now: datetime,
    hard_close_days: int = 30,
) -> bool:
    """Pure check — should the executor force-close this active turn
    before opening a new one?

    Group 6.6 Codex MAJOR #7: soft hint at 6 hours (Group 5.1) is not a
    guarantee. If the active turn is older than ``hard_close_days``
    (default 30), we close it without consulting the planner. Keeps a
    runaway open turn from carrying month-old context into a new
    conversation.

    Only applies to ``active`` turns — closed turns return False (they
    can't be re-closed).
    """
    if turn.status != "active":
        return False
    started = turn.started_at
    if started.tzinfo is None:
        # SQLite returns naive timestamps; treat as UTC to compare safely.
        started = started.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - started >= timedelta(days=hard_close_days)


__all__ = [
    "open_turn",
    "close_turn",
    "get_active_turn",
    "get_recent_closed_turns",
    "should_hard_close",
]
