"""Tests for ``user_data_change_feed`` + ``audit_outbox`` models (Sub-A11, Group 6.4).

Category I in the architecture plan: every mutation of user-facing
data (shopping items, reminders, recipes, tasks, checklists) emits
an *audit event* the planner can later read to detect changes
made through mini-app / API / system paths that didn't pass
through the chat flow.

Two tables, outbox pattern:

  ``audit_outbox``         buffer table — writes from the application
                           land here first via SAVEPOINT (so a failed
                           feed write doesn't abort the action).

  ``user_data_change_feed``  durable feed — the relay worker moves
                             rows from outbox → feed once committed.
                             Planner reads UNION(feed, outbox-pending)
                             to see the most recent activity.

Schema highlights (Category I + Codex IDEA R1):

  - ``operation_id`` is unique-per-event → makes the relay's INSERT
    idempotent under retry.
  - ``payload_hash`` is independent — same op_id with a different
    payload signals double-write of conflicting data (bug somewhere).
  - ``caused_by`` JSONB links event → originating
    plan_id/step_id/run_id (when source='среда') or
    client_session/action_id (when source='mini-app').
  - ``source`` enum restricted to {'среда','mini-app','api','system'}.

Both tables share most columns; we test them via parametrize.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models import AuditOutboxEvent, UserDataChangeFeedEvent


def _now() -> datetime:
    return datetime.now(timezone.utc)


_AUDIT_MODELS = [
    pytest.param(UserDataChangeFeedEvent, "user_data_change_feed", id="feed"),
    pytest.param(AuditOutboxEvent, "audit_outbox", id="outbox"),
]


# ---------------------------------------------------------------------------
# Basic insert / persistence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_cls,table", _AUDIT_MODELS)
def test_minimal_event_persists(
    db_session: Session, model_cls, table: str
) -> None:
    """A row with the required fields populated round-trips through
    the ORM."""
    event = model_cls(
        operation_id="op_test_001",
        tenant_id="tenant_1",
        user_id="user_1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_42",
        action="created",
        payload={"title": "молоко"},
        payload_hash="hash_abc",
        caused_by={"тип": "среда", "run_id": "run_x"},
        occurred_at=_now(),
    )
    db_session.add(event)
    db_session.commit()

    fetched = (
        db_session.query(model_cls)
        .filter_by(operation_id="op_test_001")
        .one()
    )
    assert fetched.tenant_id == "tenant_1"
    assert fetched.user_id == "user_1"
    assert fetched.source == "среда"
    assert fetched.entity_type == "shopping_list_item"
    assert fetched.entity_id == "sh_42"
    assert fetched.action == "created"
    assert fetched.payload == {"title": "молоко"}
    assert fetched.caused_by == {"тип": "среда", "run_id": "run_x"}


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_cls,table", _AUDIT_MODELS)
def test_invalid_source_rejected(
    db_session: Session, model_cls, table: str
) -> None:
    event = model_cls(
        operation_id="op_bad_source",
        tenant_id="t1",
        user_id="u1",
        source="weird_source",  # not in whitelist
        entity_type="shopping_list_item",
        entity_id="sh_1",
        action="created",
        payload={},
        occurred_at=_now(),
    )
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize("model_cls,table", _AUDIT_MODELS)
def test_invalid_action_rejected(
    db_session: Session, model_cls, table: str
) -> None:
    """Action whitelist: created / updated / deleted / skipped."""
    event = model_cls(
        operation_id="op_bad_action",
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_1",
        action="weird_action",
        payload={},
        occurred_at=_now(),
    )
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ---------------------------------------------------------------------------
# Uniqueness — operation_id idempotency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_cls,table", _AUDIT_MODELS)
def test_duplicate_operation_id_rejected(
    db_session: Session, model_cls, table: str
) -> None:
    """``UNIQUE (operation_id)`` ensures the relay's INSERT is
    idempotent under retry — same op_id collapses to one row."""
    db_session.add(
        model_cls(
            operation_id="op_uniq",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload={"v": 1},
            occurred_at=_now(),
        )
    )
    db_session.commit()

    db_session.add(
        model_cls(
            operation_id="op_uniq",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_2",  # different entity, but same op
            action="created",
            payload={"v": 2},
            occurred_at=_now(),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


# ---------------------------------------------------------------------------
# Indexes (sanity)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_cls,table", _AUDIT_MODELS)
def test_recent_index_present(
    db_session: Session, model_cls, table: str
) -> None:
    """Planner-side reads scan (tenant_id, occurred_at DESC) — must
    be indexed or the per-message hot path is slow."""
    inspector = inspect(db_session.bind)
    indexes = inspector.get_indexes(table)
    names = {ix["name"] for ix in indexes}
    expected = f"ix_{table}_tenant_recent"
    assert expected in names, (
        f"missing recency index {expected!r}; got {names}"
    )


@pytest.mark.parametrize("model_cls,table", _AUDIT_MODELS)
def test_operation_id_uniqueness_metadata(
    db_session: Session, model_cls, table: str
) -> None:
    """``operation_id`` is the relay's idempotency key — must be
    declared UNIQUE at the schema level."""
    inspector = inspect(db_session.bind)
    uniques = inspector.get_unique_constraints(table)
    indexes = inspector.get_indexes(table)
    op_unique = any(
        u["column_names"] == ["operation_id"] for u in uniques
    ) or any(
        ix["unique"] and ix["column_names"] == ["operation_id"]
        for ix in indexes
    )
    assert op_unique, (
        f"{table}.operation_id must be UNIQUE — uniques={uniques}, "
        f"unique-indexes={[ix for ix in indexes if ix['unique']]}"
    )


# ---------------------------------------------------------------------------
# Outbox-specific: enqueued_at / attempts
# ---------------------------------------------------------------------------


def test_outbox_has_retry_fields(db_session: Session) -> None:
    """``audit_outbox`` must carry relay-retry bookkeeping —
    enqueued_at, attempts, last_attempt_at — that the feed table
    doesn't need."""
    inspector = inspect(db_session.bind)
    cols = {c["name"] for c in inspector.get_columns("audit_outbox")}
    assert {"enqueued_at", "attempts", "last_attempt_at"} <= cols, (
        f"audit_outbox missing retry fields; columns={sorted(cols)}"
    )


def test_feed_does_not_carry_retry_fields(db_session: Session) -> None:
    """Conversely the feed (durable destination) has no retry
    bookkeeping — once a row lands here it's done."""
    inspector = inspect(db_session.bind)
    cols = {c["name"] for c in inspector.get_columns("user_data_change_feed")}
    assert "attempts" not in cols
    assert "enqueued_at" not in cols
