from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class AgentThread(Base):
    __tablename__ = "agent_threads"

    # UNIQUE на тройку (миграция 20260718_0085; audit 2026-07-18
    # runtime-core #5 / cross-concurrency FC-2): `_get_or_create_thread`
    # (`runtime/executor.py`) ищет ровно по (tenant_id, channel_type,
    # external_chat_id) через `one_or_none()` — дубль строки (гонка
    # get-or-create без констрейнта) ронял КАЖДЫЙ enqueue_action с
    # MultipleResultsFound навсегда. Теперь гонка = громкий IntegrityError
    # вместо необратимого дубля.
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_type",
            "external_chat_id",
            name="uq_agent_threads_tenant_channel_chat",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    assistant_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistants.id"),
        nullable=True,
        index=True,
    )
    channel_type: Mapped[str] = mapped_column(String(32), index=True)
    external_chat_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("agent_threads.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    assistant_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistants.id"),
        nullable=True,
        index=True,
    )
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    inbound_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("inbound_messages.id"),
        nullable=True,
        index=True,
    )
    # Sub-A9 (Group 6.6) — pointer to the conversation_turn this run
    # belongs to. NULL for legacy rows that pre-date the column; new
    # planner-driven runs always populate it. The composite FK on
    # (turn_id, thread_id) defined in __table_args__ keeps the run
    # from accidentally pointing at a turn in another thread.
    #
    # Codex Sub-A9 R1 MINOR #6 — index defined explicitly in
    # __table_args__ (partial WHERE turn_id IS NOT NULL) to match the
    # Alembic migration; we don't pass ``index=True`` here so the ORM
    # create_all() schema agrees with the migrated schema.
    turn_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    action_type: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    error_message_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Sub-A9 / Group 6.6 Codex MAJOR #1 — composite FK so a run
        # cannot reference a turn in a different thread. NULL-on-either-
        # column lets the constraint stay inert for legacy rows where
        # ``turn_id`` is unset (Group 6.6 backfill strategy A).
        ForeignKeyConstraint(
            ["turn_id", "thread_id"],
            ["conversation_turns.id", "conversation_turns.thread_id"],
            name="fk_agent_runs_turn_thread",
        ),
        # Codex Sub-A9 R2 MAJOR #1 — declared UNIQUE(id, turn_id) so that
        # ``planner_executions(run_id, turn_id) → agent_runs(id, turn_id)``
        # composite FK has a valid target. ``id`` is already PK and
        # globally unique, so this is a no-op for uniqueness; the
        # constraint exists only to satisfy Postgres's FK-target rule.
        UniqueConstraint(
            "id",
            "turn_id",
            name="uq_agent_runs_id_turn",
        ),
        # Codex Sub-A9 R1 MINOR #6 — partial index matches the Alembic
        # migration so create_all() and migrate-up produce the same
        # schema. We only need the index for turn_id IS NOT NULL since
        # the planner-flow always sets it; legacy NULL rows don't need
        # to be in the index.
        Index(
            "ix_agent_runs_turn_id",
            "turn_id",
            postgresql_where=sql_text("turn_id IS NOT NULL"),
            sqlite_where=sql_text("turn_id IS NOT NULL"),
        ),
    )
