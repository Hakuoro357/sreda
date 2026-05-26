"""Postgres-only FK enforcement tests for conversation_turns (Codex Sub-A9 R1 MAJOR #5).

The unit-test suite uses SQLite with ``PRAGMA foreign_keys=OFF`` by
default (it's the project's standing convention — many fixtures
insert orphan rows for test brevity). That means the composite FK
``agent_runs(turn_id, thread_id) → conversation_turns(id, thread_id)``
is *declared* in the schema but its enforcement isn't tested.

This module fills that gap by running against a real Postgres instance
with FKs always enforced. Tests verify:

  - Inserting a turn-less agent_run (turn_id=NULL) succeeds — legacy
    backfill compatibility.
  - Inserting an agent_run with turn_id pointing at a non-existent
    turn fails with IntegrityError.
  - Inserting an agent_run whose (turn_id, thread_id) crosses threads
    fails — the central invariant Group 6.6 was designed to enforce.
  - Two active turns in the same thread fail (partial unique index).

Skipped unless SREDA_TEST_POSTGRES_URL is set, the DB name passes the
test-only sanity check, and SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1.
Reuses the same safety guards as
``tests/integration/test_message_queue_postgres_concurrency.py``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models import AgentRun, AgentThread, ConversationTurn


# ---------------------------------------------------------------------------
# Safety (same shape as message_queue postgres tests — keep in sync)
# ---------------------------------------------------------------------------

_POSTGRES_URL = os.environ.get("SREDA_TEST_POSTGRES_URL")
_DESTRUCTIVE_OPT_IN = (
    os.environ.get("SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN") == "1"
)
_REMOTE_OPT_IN = os.environ.get("SREDA_TEST_POSTGRES_REMOTE_OPT_IN") == "1"
_LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "postgres", "pg", "db"}
)


def _safety_reason(url: str) -> str | None:
    """Subset-mirror of the queue tests' safety check — scheme, strict
    test-DB-name pattern, host allowlist, query-param host override."""
    if not url:
        return "url is empty"
    parsed = urlparse(url)
    if parsed.scheme not in {"postgresql", "postgres", "postgresql+psycopg"}:
        return f"scheme {parsed.scheme!r} is not postgresql"
    db_name = (parsed.path or "").lstrip("/").lower()
    if not db_name:
        return "DB name missing from URL"
    valid_name = (
        db_name == "test"
        or db_name.startswith("test_")
        or db_name.endswith("_test")
        or "_test_" in db_name
    )
    if not valid_name:
        return f"DB name {db_name!r} doesn't look like a test DB"
    url_host = (parsed.hostname or "").lower()
    if url_host not in _LOCAL_HOSTS and not _REMOTE_OPT_IN:
        return f"URL host {url_host!r} is remote without opt-in"
    query_params = parse_qs(parsed.query or "")
    for param in ("host", "hostaddr"):
        for raw_value in query_params.get(param, []):
            for override in raw_value.split(","):
                override_host = override.strip().lower()
                if override_host and override_host not in _LOCAL_HOSTS and not _REMOTE_OPT_IN:
                    return f"query param {param}={override_host!r} routes outside allowlist"
    return None


_SAFETY_FAILURE = (
    _safety_reason(_POSTGRES_URL or "") if _POSTGRES_URL else "URL not set"
)
_SAFETY_OK = (
    bool(_POSTGRES_URL)
    and _DESTRUCTIVE_OPT_IN
    and _SAFETY_FAILURE is None
)

pytestmark = pytest.mark.skipif(
    not _SAFETY_OK,
    reason=(
        "conversation_turns FK tests require both SREDA_TEST_POSTGRES_URL "
        "(scheme+host+test-DB-name) and SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1. "
        f"Failure: {_SAFETY_FAILURE or 'destructive opt-in missing'}"
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def engine():
    """Per-test engine that drops + recreates the three tables we
    touch. Tenants/workspaces/etc. are skipped because we're only
    exercising the FK from agent_runs ↔ conversation_turns; the other
    FKs on AgentThread (to tenants, workspaces) won't be checked
    because we never insert orphan rows that violate them — we use
    DEFER + skip those FKs by creating the agent_threads row first
    with stub IDs and matching parent rows seeded inline."""
    eng = create_engine(_POSTGRES_URL, echo=False, future=True)
    # Order matters — agent_runs depends on conversation_turns + agent_threads.
    with eng.begin() as conn:
        for table in ("agent_runs", "conversation_turns", "agent_threads"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        for table in (AgentThread, ConversationTurn, AgentRun):
            table.__table__.create(conn, checkfirst=True)
    yield eng
    with eng.begin() as conn:
        for table in ("agent_runs", "conversation_turns", "agent_threads"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    SessionFactory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    sessions = []

    def _make():
        s = SessionFactory()
        sessions.append(s)
        return s

    yield _make
    for s in sessions:
        s.close()


def _seed_thread(session, *, thread_id: str, tenant_id: str = "t1") -> AgentThread:
    th = AgentThread(
        id=thread_id,
        tenant_id=tenant_id,
        workspace_id="ws_test",
        channel_type="telegram",
        external_chat_id="42",
        status="active",
    )
    session.add(th)
    session.flush()
    return th


def _seed_turn(session, *, thread_id: str, tenant_id: str = "t1") -> ConversationTurn:
    turn = ConversationTurn(
        id=f"turn_{thread_id}",
        turn_seq=1,
        thread_id=thread_id,
        tenant_id=tenant_id,
        started_at=_now(),
        status="active",
        run_count=0,
        total_tokens=0,
        total_cost_usd=0,
    )
    session.add(turn)
    session.flush()
    return turn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_agent_run_with_null_turn_id_allowed(session_factory):
    """Backfill compat: legacy rows have ``turn_id=NULL`` forever; the
    composite FK must stay inert when either column is NULL."""
    session = session_factory()
    _seed_thread(session, thread_id="thread_a")
    session.add(
        AgentRun(
            id="run_legacy",
            thread_id="thread_a",
            tenant_id="t1",
            workspace_id="ws_test",
            action_type="chat",
            status="pending",
            input_json="{}",
            turn_id=None,
        )
    )
    session.commit()
    session.close()


def test_agent_run_with_dangling_turn_id_rejected(session_factory):
    """Pointing at a non-existent turn fails the FK check."""
    session = session_factory()
    _seed_thread(session, thread_id="thread_a")
    session.commit()

    session.add(
        AgentRun(
            id="run_bad",
            thread_id="thread_a",
            tenant_id="t1",
            workspace_id="ws_test",
            action_type="chat",
            status="pending",
            input_json="{}",
            turn_id="turn_does_not_exist",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()


def test_agent_run_crossing_threads_rejected(session_factory):
    """Codex Sub-A9 R1 MAJOR #5 — the heart of the composite FK
    invariant: an agent_run pointing at a real turn but in a DIFFERENT
    thread must be rejected. Without the composite FK Postgres would
    happily accept this and let the run reference a turn from another
    conversation."""
    session = session_factory()
    _seed_thread(session, thread_id="thread_a")
    _seed_thread(session, thread_id="thread_b")
    turn_a = _seed_turn(session, thread_id="thread_a")
    session.commit()

    # Run is on thread_b but turn lives in thread_a — composite FK rejects.
    session.add(
        AgentRun(
            id="run_cross",
            thread_id="thread_b",
            tenant_id="t1",
            workspace_id="ws_test",
            action_type="chat",
            status="pending",
            input_json="{}",
            turn_id=turn_a.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()


def test_agent_run_same_thread_turn_accepted(session_factory):
    """Sister test: same thread → FK passes. Confirms we're not just
    breaking the happy path."""
    session = session_factory()
    _seed_thread(session, thread_id="thread_a")
    turn = _seed_turn(session, thread_id="thread_a")
    session.commit()

    session.add(
        AgentRun(
            id="run_ok",
            thread_id="thread_a",
            tenant_id="t1",
            workspace_id="ws_test",
            action_type="chat",
            status="pending",
            input_json="{}",
            turn_id=turn.id,
        )
    )
    session.commit()
    session.close()


def test_two_active_turns_same_thread_rejected_under_real_pg(session_factory):
    """Partial unique index ``ix_one_active_turn_per_thread`` is exercised
    against real Postgres (SQLite tests already cover this — duplicate
    here for confidence under production semantics)."""
    session = session_factory()
    _seed_thread(session, thread_id="thread_a")
    _seed_turn(session, thread_id="thread_a")
    session.commit()

    # Try to insert a second active turn for the same thread.
    session.add(
        ConversationTurn(
            id="turn_dup",
            turn_seq=2,
            thread_id="thread_a",
            tenant_id="t1",
            started_at=_now(),
            status="active",
            run_count=0,
            total_tokens=0,
            total_cost_usd=0,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()
