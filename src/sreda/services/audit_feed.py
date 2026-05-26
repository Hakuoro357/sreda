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


class PayloadHashConflict(RuntimeError):
    """Raised by ``emit_event`` when an existing row for the same
    ``operation_id`` has a different ``payload_hash`` than the
    incoming write.

    Indicates either an ``operation_id`` collision (would be a bug
    in the hash construction) or a replay of the same plan-step
    with different real state. Callers MAY catch and downgrade to a
    log line if they know the conflict is benign; default is to
    propagate so the bug surfaces in monitoring (Codex Sub-A11 R2
    MAJOR #3 — was just a warning before, now a hard signal).
    """

    def __init__(
        self,
        *,
        operation_id: str,
        existing_hash: str,
        new_hash: str,
        location: str,
    ) -> None:
        super().__init__(
            f"payload_hash mismatch for op_id={operation_id!r} "
            f"({location}): existing={existing_hash!r} new={new_hash!r}"
        )
        self.operation_id = operation_id
        self.existing_hash = existing_hash
        self.new_hash = new_hash
        self.location = location


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _compute_payload_hash(payload: dict | None) -> str:
    """Canonical JSON → SHA-256 hex. Used to detect "same op_id,
    different payload" double-writes (Codex IDEA R1 MAJOR #3)."""
    if payload is None:
        return ""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _effective_hash(stored_hash: str | None, payload: dict | None) -> str:
    """Codex R3 MAJOR #2 — return the row's stored hash if present,
    otherwise recompute from payload. Closes the loophole where
    legacy/partial-data rows with ``payload_hash IS NULL`` would
    silently bypass conflict detection."""
    if stored_hash:
        return stored_hash
    return _compute_payload_hash(payload)


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
        # Codex R3+R4 MAJOR #2 — direct comparison of effective hashes,
        # no truthiness gate. `""` (genuinely empty payload on both
        # sides) matches `""` → no conflict; `""` vs a real hex hash
        # → conflict. Closes the NULL-payload silent-collapse path
        # flagged in R4.
        existing_effective = _effective_hash(existing.payload_hash, existing.payload)
        if existing_effective != new_hash:
            raise PayloadHashConflict(
                operation_id=operation_id,
                existing_hash=existing_effective,
                new_hash=new_hash,
                location="outbox",
            )
        return existing

    feed_exists = session.execute(
        select(UserDataChangeFeedEvent).where(
            UserDataChangeFeedEvent.operation_id == operation_id
        )
    ).scalar_one_or_none()
    if feed_exists is not None:
        feed_effective = _effective_hash(
            feed_exists.payload_hash, feed_exists.payload
        )
        if feed_effective != new_hash:
            raise PayloadHashConflict(
                operation_id=operation_id,
                existing_hash=feed_effective,
                new_hash=new_hash,
                location="feed",
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

    # Codex R2 CRITICAL — IntegrityError recovery is the CALLER's
    # responsibility via SAVEPOINT. The helper does NOT call
    # session.rollback() (would clobber the parent txn) and does
    # NOT wrap in session.begin_nested() (interacts poorly with the
    # test-fixture's outer transaction; SQLAlchemy 2.x nested-txn
    # semantics depend on `join_transaction_mode`).
    #
    # Recommended caller pattern (Codex R3 MINOR — catch OUTSIDE
    # the begin_nested block so the context manager rolls the
    # savepoint back before the except runs):
    #
    #     try:
    #         with session.begin_nested():
    #             emit_event(session, ...)
    #     except IntegrityError:
    #         # Concurrent write won; savepoint already rolled back.
    #         pass
    #
    # On Postgres a raw IntegrityError without savepoint will poison
    # the transaction — the caller's commit() will fail. That's
    # acceptable design: it means "either the audit + the data both
    # land, or neither does", which is the right default semantics.
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
    session.flush()
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

    Idempotent on retry — pre-check by op_id avoids the common
    redo path; the UNIQUE constraint on feed.operation_id is the
    safety net for concurrent relays.

    Race semantics (Codex Sub-A11 R3 MAJOR #1): if a concurrent
    relay inserts the same op_id between our pre-check and our
    INSERT, the IntegrityError propagates out so the caller's
    SAVEPOINT can roll back. The outbox row stays intact; the
    next tick's pre-check sees the feed row and DELETEs it
    cleanly. Caller pattern:

        try:
            with session.begin_nested():
                relay_outbox(session, batch_size=100)
        except IntegrityError:
            # transient race; next tick will retry
            pass

    Transient non-IntegrityError failures (e.g. DB connection
    blip during feed INSERT) increment ``attempts`` and bump
    ``last_attempt_at`` on the affected row; the relay leaves the
    row in outbox for the next tick.
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
        # under retry — Codex IDEA R1 / Sub-A11 design).
        feed_existing = session.execute(
            select(UserDataChangeFeedEvent).where(
                UserDataChangeFeedEvent.operation_id == outbox_event.operation_id
            )
        ).scalar_one_or_none()

        if feed_existing is not None:
            # Codex R2 MAJOR #3 + R3 MAJOR #2 — compare effective
            # hashes (stored OR recomputed) so NULL hashes don't
            # bypass conflict detection. If they differ, leave the
            # outbox row with last_error so the conflict surfaces in
            # monitoring.
            feed_hash = _effective_hash(
                feed_existing.payload_hash, feed_existing.payload
            )
            outbox_hash = _effective_hash(
                outbox_event.payload_hash, outbox_event.payload
            )
            # Codex R4 MAJOR #1 — direct compare, no truthiness gate.
            if feed_hash != outbox_hash:
                outbox_event.attempts += 1
                outbox_event.last_attempt_at = when
                outbox_event.last_error = (
                    f"payload_hash mismatch: feed={feed_hash!r} "
                    f"outbox={outbox_hash!r}"
                )
                session.flush()
                _logger.error(
                    "relay_outbox: payload_hash mismatch for op_id=%s "
                    "(feed already has different content); leaving outbox "
                    "row with last_error for triage.",
                    outbox_event.operation_id,
                )
                continue
            # Same hash — already relayed; drop the outbox row.
            session.delete(outbox_event)
            session.flush()
            relayed += 1
            continue

        # Codex R2 CRITICAL — INSERT is plain (no begin_nested) for
        # the same reason as emit_event: the relay's caller (typically
        # a background worker tick with its own session lifecycle)
        # is expected to wrap the relay call in its own SAVEPOINT
        # if it wants per-row isolation. The pre-check above closes
        # the common race path (op_id already in feed) cheaply.
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
        except IntegrityError:
            # Codex R3 MAJOR #1 — propagate so the caller's
            # SAVEPOINT can roll back and they decide whether to
            # retry the batch in a new transaction. Swallowing and
            # returning would leave the session poisoned silently
            # — caller's commit() would fail later with a confusing
            # error far from the root cause. The outbox row stays
            # intact (savepoint rollback restores it); the next
            # tick's pre-check sees the feed row and DELETEs it
            # cleanly.
            _logger.warning(
                "relay_outbox: race on op_id=%s (concurrent relay?); "
                "raising for caller's savepoint to handle.",
                outbox_event.operation_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "relay_outbox: failed to move op_id=%s; will retry next tick",
                outbox_event.operation_id,
            )
            # Mark the row for retry — UPDATE lands in same txn.
            outbox_event.attempts += 1
            outbox_event.last_attempt_at = when
            outbox_event.last_error = repr(exc)[:1000]
            session.flush()

    return relayed


__all__ = [
    "PayloadHashConflict",
    "emit_event",
    "read_recent_events",
    "relay_outbox",
]
