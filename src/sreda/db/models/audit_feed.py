"""``user_data_change_feed`` + ``audit_outbox`` (Sub-A11, Category I / Group 6.4).

Two tables that together implement the outbox pattern for the
data-change audit feed described in Category I of the architecture plan:

  ``audit_outbox``         buffer table. Application writes events
                           here in the same DB transaction as the
                           underlying data mutation (via SAVEPOINT
                           so a failed feed write doesn't abort the
                           parent action — Codex IDEA R1).

  ``user_data_change_feed``  durable feed. A relay worker periodically
                             moves rows from outbox → feed and deletes
                             them from outbox. Planner reads
                             ``UNION(feed, audit_outbox-pending)`` to
                             see the most recent activity across all
                             sources (chat / mini-app / api / system).

Both tables share the same event shape — only the bookkeeping fields
(``enqueued_at``, ``attempts``, ``last_attempt_at``) differ. We
declare a small mixin so the column list stays single-sourced.

Fields:

  operation_id     SHA-1 of the originating tool operation. UNIQUE
                   makes the relay's INSERT idempotent under retry.
  tenant_id, user_id   scope
  source           {'среда','mini-app','api','system'} — what
                   originated the mutation
  entity_type      whitelist of entity types we care about
                   (shopping_list_item / family_reminder / task /
                   recipe / checklist / ...)
  entity_id        the affected row id; nullable for batch ops
                   that don't map 1:1 to a single row
  action           {'created','updated','deleted','skipped'}
  payload          JSONB — the after-image for created/updated, the
                   before-image for deleted, or a diff for partial
                   updates. Encrypted at app layer when it carries
                   PII (caller's responsibility — the column type
                   is plain JSONB here so it can be indexed).
  payload_hash     SHA-256 hex of canonical-JSON payload. Lets the
                   relay detect "same op_id, different payload" =
                   bug (Codex IDEA R1 MAJOR #3).
  caused_by        JSONB linking the event to its originating context:
                     - source='среда':  {тип, run_id, plan_id, step_id}
                     - source='mini-app': {client_session, action_id?}
                     - source='api':    {request_id}
                     - source='system': null (migrations, cleanup,
                                              prod SQL admin)
  occurred_at      timestamp the underlying action happened (not
                   when the row was inserted into this table).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from sreda.db.base import Base


_JSONB = JSON().with_variant(JSONB(), "postgresql")


# Source / action whitelists. Embedded in the CHECK constraints; kept
# here as module constants for re-use by the service layer.
_AUDIT_SOURCES = ("среда", "mini-app", "api", "system")
_AUDIT_ACTIONS = ("created", "updated", "deleted", "skipped")

_SOURCE_CHECK = (
    "source IN ('"
    + "','".join(_AUDIT_SOURCES)
    + "')"
)
_ACTION_CHECK = (
    "action IN ('"
    + "','".join(_AUDIT_ACTIONS)
    + "')"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserDataChangeFeedEvent(Base):
    """Durable audit feed — relay worker writes here from outbox."""

    __tablename__ = "user_data_change_feed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    source: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)

    payload: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caused_by: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            name="uq_user_data_change_feed_operation_id",
        ),
        CheckConstraint(_SOURCE_CHECK, name="ck_user_data_change_feed_source"),
        CheckConstraint(_ACTION_CHECK, name="ck_user_data_change_feed_action"),
        Index(
            "ix_user_data_change_feed_tenant_recent",
            "tenant_id",
            "occurred_at",
        ),
        # Per-entity lookup so "what changed about sh_42 lately?" is fast.
        Index(
            "ix_user_data_change_feed_entity",
            "tenant_id",
            "entity_type",
            "entity_id",
            "occurred_at",
        ),
    )


class AuditOutboxEvent(Base):
    """Outbox buffer — relay drains rows from here into the feed."""

    __tablename__ = "audit_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    source: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)

    payload: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caused_by: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )

    # Outbox-specific retry bookkeeping — feed table doesn't carry these.
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            name="uq_audit_outbox_operation_id",
        ),
        CheckConstraint(_SOURCE_CHECK, name="ck_audit_outbox_source"),
        CheckConstraint(_ACTION_CHECK, name="ck_audit_outbox_action"),
        Index(
            "ix_audit_outbox_tenant_recent",
            "tenant_id",
            "occurred_at",
        ),
        # Relay-drain query: oldest-pending-first.
        Index(
            "ix_audit_outbox_pending",
            "enqueued_at",
        ),
    )


__all__ = [
    "AuditOutboxEvent",
    "UserDataChangeFeedEvent",
    "_AUDIT_ACTIONS",
    "_AUDIT_SOURCES",
]
