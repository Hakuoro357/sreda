"""Per-thread FIFO queue (Sub-A2 of Plan-Execute Epic vex-assistant#74).

Each row is one inbound message awaiting processing by a worker. The
``thread_id`` column is the FIFO key: only one job per ``thread_id`` may
be ``status='processing'`` with an active lease at a time. Different
threads of the same tenant (DM + group, MAX + Telegram) process in
parallel.

Cross-channel idempotency lives in the composite ``UNIQUE (channel,
external_update_id)`` constraint — Telegram redelivery and equivalent
MAX retries are both swallowed at INSERT time without ever reaching
the worker loop.

Lease fencing fields (``worker_id`` / ``attempt`` / ``lease_expires_at``)
back the conditional-UPDATE pattern that prevents a slow original
worker from racing the retry worker (see ``workers/message_worker.py``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from sreda.db.base import Base


# Cross-dialect JSON column type. Postgres dialect uses JSONB (indexable,
# native operators); SQLite (tests) falls back to TEXT-backed JSON.
_JSONB = JSON().with_variant(JSONB(), "postgresql")


class MessageJob(Base):
    """One enqueued message awaiting worker processing.

    See module docstring for FIFO semantics and lease-fencing details.
    """

    __tablename__ = "message_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_update_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_payload: Mapped[dict] = mapped_column(_JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
    )
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    worker_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    __table_args__ = (
        # Cross-channel dedup. Telegram redelivery, MAX retry, future
        # channels — all collapse on the same composite key.
        UniqueConstraint(
            "channel",
            "external_update_id",
            name="uq_message_jobs_channel_external_update_id",
        ),
        # Enum check — keeps state machine values honest.
        CheckConstraint(
            "status IN ('pending','processing','done','failed','dead_letter')",
            name="ck_message_jobs_status_enum",
        ),
        # State / timestamp consistency:
        # - pending: no start, no finish
        # - processing: started, no finish, lease set
        # - done/failed/dead_letter: finished
        CheckConstraint(
            "("
            " (status = 'pending'    AND started_at IS NULL  AND finished_at IS NULL)"
            " OR (status = 'processing' AND started_at IS NOT NULL AND finished_at IS NULL "
            "     AND lease_expires_at IS NOT NULL)"
            " OR (status IN ('done','failed','dead_letter') AND finished_at IS NOT NULL)"
            ")",
            name="ck_message_jobs_status_timestamps",
        ),
        # Partial index for the FIFO claim query — narrow on pending rows.
        Index(
            "ix_message_jobs_pending",
            "thread_id",
            "enqueued_at",
            postgresql_where=sql_text("status = 'pending'"),
            sqlite_where=sql_text("status = 'pending'"),
        ),
        # Partial index for the in-flight check (NOT EXISTS clause in claim).
        Index(
            "ix_message_jobs_processing",
            "thread_id",
            postgresql_where=sql_text("status = 'processing'"),
            sqlite_where=sql_text("status = 'processing'"),
        ),
        # Partial index for failover scan — find expired leases quickly.
        Index(
            "ix_message_jobs_expired_lease",
            "lease_expires_at",
            postgresql_where=sql_text("status = 'processing'"),
            sqlite_where=sql_text("status = 'processing'"),
        ),
        # Tenant-wide analytics (cross-thread queries — admin pages, GEPA).
        Index(
            "ix_message_jobs_tenant_analytics",
            "tenant_id",
            "enqueued_at",
        ),
    )
