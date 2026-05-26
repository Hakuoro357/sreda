"""Tests for ``sreda.services.audit_feed`` (Sub-A11, Category I).

Service-layer behavior on top of the ``audit_outbox`` +
``user_data_change_feed`` schemas. We test:

  emit_event           — happy path, dedup, validation
  read_recent_events   — UNION feed + outbox, ordering, time filter
  relay_outbox         — drain + idempotent retry
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from sreda.db.models import AuditOutboxEvent, UserDataChangeFeedEvent
from sreda.services.audit_feed import (
    PayloadHashConflict,
    emit_event,
    read_recent_events,
    relay_outbox,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# emit_event
# ---------------------------------------------------------------------------


def test_emit_event_writes_outbox_row(db_session: Session) -> None:
    event = emit_event(
        db_session,
        operation_id="op_emit_1",
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_1",
        action="created",
        payload={"title": "молоко"},
        caused_by={"тип": "среда", "run_id": "run_a", "plan_id": "plan_a"},
    )
    db_session.commit()

    assert event.operation_id == "op_emit_1"
    # Hash filled by helper.
    assert event.payload_hash and len(event.payload_hash) == 64

    fetched = (
        db_session.query(AuditOutboxEvent)
        .filter_by(operation_id="op_emit_1")
        .one()
    )
    assert fetched.entity_id == "sh_1"
    assert fetched.payload == {"title": "молоко"}


def test_emit_event_idempotent_returns_existing(db_session: Session) -> None:
    """Re-emit with the SAME op_id AND SAME payload (true retry) →
    returns the existing row without duplicating."""
    first = emit_event(
        db_session,
        operation_id="op_idem",
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_1",
        action="created",
        payload={"v": 1},
    )
    db_session.commit()

    second = emit_event(
        db_session,
        operation_id="op_idem",
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_1",
        action="created",
        payload={"v": 1},  # IDENTICAL payload — true retry
    )
    assert second.id == first.id


def test_emit_event_rejects_bad_entity_type(db_session: Session) -> None:
    """Codex R1 MAJOR #7 — entity_type whitelist enforced
    synchronously."""
    with pytest.raises(ValueError, match="entity_type="):
        emit_event(
            db_session,
            operation_id="op_bad_entity",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="not_a_real_entity",
            entity_id="x",
            action="created",
        )


def test_emit_event_rejects_bad_source(db_session: Session) -> None:
    with pytest.raises(ValueError, match="source="):
        emit_event(
            db_session,
            operation_id="op_bad",
            tenant_id="t1",
            user_id="u1",
            source="not_in_whitelist",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
        )


def test_emit_event_hash_mismatch_raises(db_session: Session) -> None:
    """Codex R2 MAJOR #3 — same op_id with different payload must
    raise PayloadHashConflict rather than silently collapse."""
    emit_event(
        db_session,
        operation_id="op_hash_conflict",
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_1",
        action="created",
        payload={"v": 1},
    )
    db_session.commit()

    with pytest.raises(PayloadHashConflict) as excinfo:
        emit_event(
            db_session,
            operation_id="op_hash_conflict",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload={"v": 2},  # different payload, same op_id
        )
    assert excinfo.value.operation_id == "op_hash_conflict"
    assert excinfo.value.location == "outbox"


def test_emit_event_hash_mismatch_with_null_payload(
    db_session: Session,
) -> None:
    """Codex R4 MAJOR — when the existing row has BOTH payload=None
    AND payload_hash=NULL, conflict detection must still fire if
    the new emit has a non-NULL payload (their effective hashes
    differ: "" vs sha256(...)). R3's truthiness-gated check
    silently collapsed this case."""
    db_session.add(
        AuditOutboxEvent(
            operation_id="op_null_payload",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload=None,         # legacy null payload
            payload_hash=None,
            occurred_at=_now(),
        )
    )
    db_session.commit()

    with pytest.raises(PayloadHashConflict):
        emit_event(
            db_session,
            operation_id="op_null_payload",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload={"new": "data"},
        )


def test_emit_event_both_null_payloads_no_conflict(
    db_session: Session,
) -> None:
    """Genuine retry with no payload on both sides → no conflict.
    "" == "" so the equality check passes."""
    db_session.add(
        AuditOutboxEvent(
            operation_id="op_both_null",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload=None,
            payload_hash=None,
            occurred_at=_now(),
        )
    )
    db_session.commit()

    # Same op_id, both payload=None — must NOT raise.
    result = emit_event(
        db_session,
        operation_id="op_both_null",
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_1",
        action="created",
        payload=None,
    )
    assert result.operation_id == "op_both_null"


def test_emit_event_hash_mismatch_with_null_stored_hash(
    db_session: Session,
) -> None:
    """Codex R3 MAJOR #2 — when the existing row's stored payload_hash
    is NULL (legacy/partial-data path), conflict detection must
    recompute the hash from the stored payload and still surface
    the mismatch. Without this, NULL-hash rows would silently
    swallow different-payload retries."""
    # Insert an outbox row directly with payload_hash=NULL.
    db_session.add(
        AuditOutboxEvent(
            operation_id="op_null_hash",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload={"v": 1},
            payload_hash=None,  # legacy state
            occurred_at=_now(),
        )
    )
    db_session.commit()

    with pytest.raises(PayloadHashConflict):
        emit_event(
            db_session,
            operation_id="op_null_hash",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload={"v": 2},  # different — must conflict
        )


def test_emit_event_hash_mismatch_feed_side_raises(db_session: Session) -> None:
    """Same conflict, but the prior write is already in the feed
    (not outbox). Still raises with location='feed'."""
    db_session.add(
        UserDataChangeFeedEvent(
            operation_id="op_hash_feed",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload={"v": 1},
            payload_hash="hash_v1",
            occurred_at=_now(),
        )
    )
    db_session.commit()

    with pytest.raises(PayloadHashConflict) as excinfo:
        emit_event(
            db_session,
            operation_id="op_hash_feed",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload={"v": 2},
        )
    assert excinfo.value.location == "feed"


def test_emit_event_rejects_bad_action(db_session: Session) -> None:
    with pytest.raises(ValueError, match="action="):
        emit_event(
            db_session,
            operation_id="op_bad_act",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="exploded",
        )


# ---------------------------------------------------------------------------
# read_recent_events
# ---------------------------------------------------------------------------


def test_recent_events_unions_feed_and_outbox(db_session: Session) -> None:
    """Recent reads must see both the durable feed AND the pending
    outbox so the planner has the freshest picture."""
    t0 = _now() - timedelta(minutes=10)
    # Pre-existing feed row (already relayed).
    db_session.add(
        UserDataChangeFeedEvent(
            operation_id="op_feed_1",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_old",
            action="created",
            payload={"k": "old"},
            occurred_at=t0,
        )
    )
    db_session.commit()

    # New outbox row (pending relay).
    emit_event(
        db_session,
        operation_id="op_outbox_1",
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_new",
        action="created",
        payload={"k": "new"},
        occurred_at=t0 + timedelta(minutes=5),
    )
    db_session.commit()

    rows = read_recent_events(db_session, tenant_id="t1", since=t0 - timedelta(hours=1))
    op_ids = [r["operation_id"] for r in rows]
    assert "op_feed_1" in op_ids
    assert "op_outbox_1" in op_ids
    # Outbox event is newer — should sort last.
    assert op_ids.index("op_outbox_1") > op_ids.index("op_feed_1")


def test_recent_events_dedupes_by_operation_id(db_session: Session) -> None:
    """If an op_id appears in both tables (e.g. relay just inserted
    into feed but didn't delete from outbox yet), return one entry."""
    t0 = _now()
    db_session.add(
        UserDataChangeFeedEvent(
            operation_id="op_both",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload={"v": 1},
            occurred_at=t0,
        )
    )
    db_session.add(
        AuditOutboxEvent(
            operation_id="op_both",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            payload={"v": 1},
            occurred_at=t0,
        )
    )
    db_session.commit()

    rows = read_recent_events(db_session, tenant_id="t1", since=t0 - timedelta(hours=1))
    op_ids = [r["operation_id"] for r in rows]
    assert op_ids.count("op_both") == 1


def test_recent_events_scoped_by_tenant(db_session: Session) -> None:
    t0 = _now()
    emit_event(
        db_session,
        operation_id="op_t1",
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_1",
        action="created",
        occurred_at=t0,
    )
    emit_event(
        db_session,
        operation_id="op_t2",
        tenant_id="t2",
        user_id="u_other",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_99",
        action="created",
        occurred_at=t0,
    )
    db_session.commit()

    rows_t1 = read_recent_events(db_session, tenant_id="t1", since=t0 - timedelta(hours=1))
    rows_t2 = read_recent_events(db_session, tenant_id="t2", since=t0 - timedelta(hours=1))
    assert [r["operation_id"] for r in rows_t1] == ["op_t1"]
    assert [r["operation_id"] for r in rows_t2] == ["op_t2"]


# ---------------------------------------------------------------------------
# relay_outbox
# ---------------------------------------------------------------------------


def test_relay_moves_outbox_to_feed(db_session: Session) -> None:
    """Happy path: relay drains outbox rows into the feed, deleting
    them from outbox."""
    t0 = _now()
    for i in range(3):
        emit_event(
            db_session,
            operation_id=f"op_drain_{i}",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id=f"sh_{i}",
            action="created",
            occurred_at=t0 + timedelta(seconds=i),
        )
    db_session.commit()

    relayed = relay_outbox(db_session, batch_size=10)
    db_session.commit()

    assert relayed == 3
    assert db_session.query(AuditOutboxEvent).count() == 0
    assert db_session.query(UserDataChangeFeedEvent).count() == 3


def test_relay_idempotent_on_double_run(db_session: Session) -> None:
    """If relay runs twice on the same outbox row (e.g. transient
    crash mid-cycle), the second run is a no-op via the UNIQUE
    operation_id constraint on the feed."""
    t0 = _now()
    emit_event(
        db_session,
        operation_id="op_replay",
        tenant_id="t1",
        user_id="u1",
        source="среда",
        entity_type="shopping_list_item",
        entity_id="sh_1",
        action="created",
        occurred_at=t0,
    )
    # Also seed feed with the same op_id (simulating "already relayed
    # but outbox row still hanging around due to crash before delete").
    db_session.add(
        UserDataChangeFeedEvent(
            operation_id="op_replay",
            tenant_id="t1",
            user_id="u1",
            source="среда",
            entity_type="shopping_list_item",
            entity_id="sh_1",
            action="created",
            occurred_at=t0,
        )
    )
    db_session.commit()

    relayed = relay_outbox(db_session, batch_size=10)
    db_session.commit()

    # Outbox row dropped even though feed row already existed.
    assert relayed == 1
    assert db_session.query(AuditOutboxEvent).count() == 0
    # Feed still has exactly one (no duplicate created).
    assert db_session.query(UserDataChangeFeedEvent).count() == 1
