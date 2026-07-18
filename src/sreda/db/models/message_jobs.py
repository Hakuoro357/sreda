"""Per-thread FIFO queue (Sub-A2 of Plan-Execute Epic vex-assistant#74).

Each row is one inbound message awaiting processing by a worker. The
``thread_id`` column is the FIFO key: only one job per ``thread_id`` may
be ``status='processing'`` with an active lease at a time. Different
threads of the same tenant (DM + group, MAX + Telegram) process in
parallel.

Cross-channel idempotency lives in the composite ``UNIQUE (channel,
external_update_id, bot_key)`` constraint (migration 20260718_0085) —
Telegram redelivery and equivalent MAX retries are both swallowed at
INSERT time without ever reaching the worker loop. ``bot_key`` is part
of the key because Telegram ``update_id`` counters are independent
per-bot: update 42 of bot ``sreda`` and update 42 of bot ``sreda_home``
are DIFFERENT events (same defect class as fixed for
``inbound_messages`` by migration 20260603_0048; audit 2026-07-18
db-migrations #1).

``message_payload`` хранит ПОЛНЫЙ raw webhook update (текст сообщения,
имена, chat-данные) — это PII. Шифруется через ``JSONEncryptedString``
(AES-256-GCM, envelope ``v2:``; legacy plaintext JSON читается
прозрачно), как и остальные payload'ы переписки в системе (audit
2026-07-18 cross-security N1). Retention покрыт: терминальные строки
(done/failed/dead_letter) чистятся в ``maintenance/retention_cleanup.py``
(``MESSAGE_JOBS_DAYS = 30``, cutoff по ``enqueued_at``, audit-fix
2026-07-18).

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
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import JSONEncryptedString


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
    # Which bot received this update (20260718_0085). NOT NULL + default
    # ("sreda" = legacy single-bot): production producer
    # (``workers/message_queue.enqueue_message``) обязан выставлять bot_key
    # ЯВНО — default это только safety net, чтобы insert без bot_key не
    # нарушал NOT NULL (тот же контракт, что у ``OutboxMessage.bot_key``).
    # Без явного bot_key дедуп-ключ снова схлопнет два бота в один
    # namespace — см. docstring модуля.
    bot_key: Mapped[str] = mapped_column(
        String(64), nullable=False, default="sreda",
        server_default=sql_text("'sreda'"),
    )
    # Полный raw webhook update — PII, зашифрован через JSONEncryptedString
    # (cross-security N1, audit 2026-07-18). ORM отдаёт/принимает dict как
    # раньше; в БД лежит ``v2:``-шифротекст. Строки, записанные ДО
    # миграции 0085 (plaintext JSON), читаются прозрачно (envelope-sniffing).
    message_payload: Mapped[dict] = mapped_column(JSONEncryptedString(), nullable=False)
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
        # Cross-channel + cross-bot dedup (20260718_0085): Telegram
        # redelivery, MAX retry, future channels — all collapse on the
        # same composite key. bot_key обязателен в ключе: update_id
        # независимы per-bot (см. docstring модуля).
        UniqueConstraint(
            "channel",
            "external_update_id",
            "bot_key",
            name="uq_message_jobs_channel_bot_update",
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
