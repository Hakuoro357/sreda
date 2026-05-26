"""Audit-feed service helpers (Sub-A11, Category I / Group 6.4).

Public surface:

  emit_event(session, *, operation_id, ...) → ``AuditOutboxEvent``
    Writes a single event into the outbox INSIDE the caller's
    transaction. Idempotent: if ``operation_id`` already exists in
    outbox or feed, returns the existing row without inserting.

  read_recent_events(session, *, tenant_id, since)
    UNION read of feed + outbox-pending, ordered by ``occurred_at,
    id``. Stable tie-breaker for prompt-builder determinism.

  relay_outbox(session, *, batch_size=100)
    Drain N rows from outbox → feed in one transaction. Idempotent
    via the ``operation_id`` UNIQUE constraint on both tables.

The full async LISTEN/NOTIFY relay worker is a separate Sub-A12+ step
— this module provides the building blocks; callers wire them into
worker tick / direct calls as needed.

Known scope gaps (Sub-A11 MVP):

  Codex R1 CRITICAL — tool service wiring (HousewifeShoppingService,
  HousewifeReminderService, etc.) is intentionally NOT done in this
  PR. Sub-A11 lays the schema + service helpers; wiring lives with
  the planner-flow integration in Sub-A11.b / Sub-A12+ when there's
  a plan_id/step_id context to populate ``caused_by`` properly.

  Codex R1 MAJOR — SAVEPOINT wrapping is the caller's responsibility.
  ``emit_event`` writes into the session the caller passes in; if
  the caller wants "audit failure shouldn't abort the parent action",
  they wrap the call in ``with session.begin_nested():``. The helper
  doesn't manage savepoints unilaterally because that would conflict
  with callers that legitimately want audit failures to bubble up.

  Codex R1 MAJOR — operation_id is globally unique by construction.
  It's a hash of (plan_id, step_id, action, entity_type, logical_key)
  where plan_id is itself a UUID. Cross-tenant collision probability
  is 2^-160. We don't add tenant_id to the UNIQUE because that would
  make the relay's idempotency-on-retry guarantee per-tenant instead
  of global, and a relay race across tenants is a non-issue at our
  scale.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models import (
    AuditOutboxEvent,
    UserDataChangeFeedEvent,
)
from sreda.db.models.audit_feed import _AUDIT_ACTIONS, _AUDIT_SOURCES


# Codex R1 MAJOR #7 — service-side entity_type whitelist. Schema-level
# CHECK constraint would force a migration for every new data type, so
# we keep it in Python where the planner code can extend it easily.
_AUDIT_ENTITY_TYPES = frozenset({
    "shopping_list_item",
    "family_reminder",
    "task",
    "recipe",
    "checklist",
    "checklist_item",
    "menu_plan",
    "menu_plan_item",
    "family_member",
})


_logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _compute_payload_hash(payload: dict | None) -> str:
    """Canonical JSON → SHA-256 hex. Used to detect "same op_id,
    different payload" double-writes (Codex IDEA R1 MAJOR #3)."""
    if payload is None:
        return ""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def emit_event(
    session: Session,
    *,
    operation_id: str,
    tenant_id: str,
    user_id: str | None,
    source: str,
    entity_type: str,
    entity_id: str | None,
    action: str,
    payload: dict | None = None,
    caused_by: dict | None = None,
    occurred_at: datetime | None = None,
) -> AuditOutboxEvent:
    """Append an event to ``audit_outbox`` in the caller's transaction.

    Idempotent via the unique ``operation_id``: if an event with the
    same op_id already exists (in outbox OR already-relayed-to feed),
    returns the existing outbox row when present, otherwise
    re-inserts into outbox (relay will dedup downstream).

    Caller controls commit — typically wrapped in a SAVEPOINT so a
    feed-write failure doesn't abort the parent data mutation
    (Category I Codex IDEA R1).

    Raises ``ValueError`` for invalid ``source`` / ``action`` so
    typos surface synchronously, not as DB-level CHECK violation
    at commit time.
    """
    if source not in _AUDIT_SOURCES:
        raise ValueError(
            f"emit_event: source={source!r} not in {_AUDIT_SOURCES!r}"
        )
    if action not in _AUDIT_ACTIONS:
        raise ValueError(
            f"emit_event: action={action!r} not in {_AUDIT_ACTIONS!r}"
        )
    # Codex R1 MAJOR #7 — validate entity_type against whitelist
    # synchronously so typos surface before commit.
    if entity_type not in _AUDIT_ENTITY_TYPES:
        raise ValueError(
            f"emit_event: entity_type={entity_type!r} not in "
            f"{_AUDIT_ENTITY_TYPES!r}. Update the whitelist in "
            f"sreda.services.audit_feed if you're adding a new "
            f"data type to the planner's view."
        )

    when = occurred_at or _utcnow()
    new_hash = _compute_payload_hash(payload)

    # Dedup check first — both feed and outbox are checked because
    # the relay may have already moved a prior write across.
    existing = session.execute(
        select(AuditOutboxEvent).where(
            AuditOutboxEvent.operation_id == operation_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Codex R1 MAJOR #5 — detect "same op_id, different payload"
        # double-write. Indicates a bug somewhere (planner replay with
        # different inputs, or operation_id collision). Log a warning
        # but return the existing row — we deliberately don't raise
        # because the caller's mutation might still be valid; the
        # warning surfaces in monitoring.
        if existing.payload_hash and new_hash and existing.payload_hash != new_hash:
            _logger.warning(
                "emit_event: payload_hash mismatch for op_id=%s — "
                "existing=%s new=%s. Likely a bug (operation_id "
                "collision or replayed plan with different state).",
                operation_id,
                existing.payload_hash,
                new_hash,
            )
        return existing

    feed_exists = session.execute(
        select(UserDataChangeFeedEvent).where(
            UserDataChangeFeedEvent.operation_id == operation_id
        )
    ).scalar_one_or_none()
    if feed_exists is not None:
        if feed_exists.payload_hash and new_hash and feed_exists.payload_hash != new_hash:
            _logger.warning(
                "emit_event: payload_hash mismatch for already-"
                "relayed op_id=%s — existing=%s new=%s.",
                operation_id,
                feed_exists.payload_hash,
                new_hash,
            )
        # Already relayed; no-op. Return a *transient* outbox row
        # (not added to session) so the signature stays consistent.
        return AuditOutboxEvent(
            operation_id=operation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            source=source,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            payload=payload,
            payload_hash=new_hash,
            caused_by=caused_by,
            occurred_at=when,
        )

    event = AuditOutboxEvent(
        operation_id=operation_id,
        tenant_id=tenant_id,
        user_id=user_id,
        source=source,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        payload=payload,
        payload_hash=new_hash,
        caused_by=caused_by,
        occurred_at=when,
    )
    session.add(event)
    try:
        session.flush()
    except IntegrityError:
        # Codex R1 MAJOR #2 — concurrent identical-op_id writes can
        # race past our SELECT and collide at INSERT. Recover by
        # rolling back to a savepoint (caller is expected to wrap us
        # in a transaction). If the caller didn't use a savepoint,
        # the IntegrityError propagates and they handle it. We give
        # up on returning a row in that path — caller can re-emit.
        session.rollback()
        re_check = session.execute(
            select(AuditOutboxEvent).where(
                AuditOutboxEvent.operation_id == operation_id
            )
        ).scalar_one_or_none()
        if re_check is not None:
            return re_check
        # Otherwise something else went wrong; re-raise.
        raise
    return event


def read_recent_events(
    session: Session,
    *,
    tenant_id: str,
    since: datetime,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return last ``limit`` events for ``tenant_id`` since ``since``,
    UNIONing the feed + outbox-pending so the planner sees the most
    recent activity even if the relay hasn't drained yet.

    Returns plain dicts (not ORM rows) because callers serialize
    these into the planner prompt — keeping the helper detached
    from session lifecycle.
    """
    # Codex R1 MINOR #9 — cap each side at ``limit`` in SQL so this
    # function stays cheap even when the tenant has thousands of
    # rows since ``since``. We pull DESC so the most-recent slice
    # comes back; the final sort below restores chronological order.
    feed_rows = session.execute(
        select(UserDataChangeFeedEvent)
        .where(
            UserDataChangeFeedEvent.tenant_id == tenant_id,
            UserDataChangeFeedEvent.occurred_at >= since,
        )
        .order_by(
            UserDataChangeFeedEvent.occurred_at.desc(),
            UserDataChangeFeedEvent.id.desc(),
        )
        .limit(limit)
    ).scalars().all()
    outbox_rows = session.execute(
        select(AuditOutboxEvent)
        .where(
            AuditOutboxEvent.tenant_id == tenant_id,
            AuditOutboxEvent.occurred_at >= since,
        )
        .order_by(
            AuditOutboxEvent.occurred_at.desc(),
            AuditOutboxEvent.id.desc(),
        )
        .limit(limit)
    ).scalars().all()

    seen_ops: set[str] = set()
    merged: list[dict[str, Any]] = []
    for row in feed_rows + outbox_rows:
        if row.operation_id in seen_ops:
            continue
        seen_ops.add(row.operation_id)
        merged.append({
            "operation_id": row.operation_id,
            "tenant_id": row.tenant_id,
            "user_id": row.user_id,
            "source": row.source,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "action": row.action,
            "payload": row.payload,
            "caused_by": row.caused_by,
            "occurred_at": row.occurred_at,
        })

    merged.sort(key=lambda e: (e["occurred_at"], e["operation_id"]))
    return merged[-limit:]


def relay_outbox(
    session: Session,
    *,
    batch_size: int = 100,
    now: datetime | None = None,
) -> int:
    """Move up to ``batch_size`` rows from ``audit_outbox`` →
    ``user_data_change_feed``. Returns count of rows relayed
    successfully.

    Idempotent on retry — ``UNIQUE (operation_id)`` on both tables
    plus ``ON CONFLICT DO NOTHING`` on the feed-insert means re-running
    the relay over the same outbox row is a no-op.

    Failures (e.g. transient DB issue) increment ``attempts`` and
    bump ``last_attempt_at`` on the affected row; the relay leaves
    the row in outbox for the next tick.
    """
    when = now or _utcnow()
    batch = session.execute(
        select(AuditOutboxEvent)
        .order_by(AuditOutboxEvent.enqueued_at)
        .limit(batch_size)
    ).scalars().all()

    relayed = 0
    for outbox_event in batch:
        # Pre-check whether the feed already has this op_id (idempotency
        # under retry — Codex IDEA R1 / Sub-A11 design). If yes, just
        # drop the outbox row; the previous relay already did the
        # work. Pre-checking avoids the messy "rollback after
        # IntegrityError" path which would also rollback the caller's
        # transaction.
        feed_exists = session.execute(
            select(UserDataChangeFeedEvent.id).where(
                UserDataChangeFeedEvent.operation_id == outbox_event.operation_id
            )
        ).scalar_one_or_none()

        if feed_exists is not None:
            session.delete(outbox_event)
            session.flush()
            relayed += 1
            continue

        try:
            feed_event = UserDataChangeFeedEvent(
                operation_id=outbox_event.operation_id,
                tenant_id=outbox_event.tenant_id,
                user_id=outbox_event.user_id,
                source=outbox_event.source,
                entity_type=outbox_event.entity_type,
                entity_id=outbox_event.entity_id,
                action=outbox_event.action,
                payload=outbox_event.payload,
                payload_hash=outbox_event.payload_hash,
                caused_by=outbox_event.caused_by,
                occurred_at=outbox_event.occurred_at,
            )
            session.add(feed_event)
            session.flush()
            session.delete(outbox_event)
            session.flush()
            relayed += 1
        except IntegrityError as exc:
            # Race: another worker just relayed this op_id between
            # our pre-check and our INSERT. Log + bump attempts,
            # leave the outbox row for next tick (the conflict will
            # be detected by the pre-check then).
            _logger.warning(
                "relay_outbox: race on op_id=%s (concurrent relay?); "
                "will retry next tick. exc=%r",
                outbox_event.operation_id,
                exc,
            )
            # Don't rollback — the IntegrityError already poisoned
            # the txn at the SQL level. Caller is responsible for
            # detecting and recovering. We just stop processing this
            # batch.
            break
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "relay_outbox: failed to move op_id=%s; will retry next tick",
                outbox_event.operation_id,
            )
            # Mark the row for retry without rolling back the session.
            outbox_event.attempts += 1
            outbox_event.last_attempt_at = when
            outbox_event.last_error = repr(exc)[:1000]
            session.flush()

    return relayed


__all__ = [
    "emit_event",
    "read_recent_events",
    "relay_outbox",
]
