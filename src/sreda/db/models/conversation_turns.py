"""``conversation_turns`` table — per-thread thematic turn entity.

Sub-A9 of Plan-Execute Epic (Hakuoro357/vex-assistant#74).

Lives *inside* an ``agent_thread``. One ``agent_thread`` (one physical
channel chat) → many ``conversation_turns`` (each is a coherent
thematic exchange — opened when planner returns ``is_new_turn=true``,
closed when the next turn starts or hard-close fires).

Group 6.6 semantics:

  - id              ``turn_<uuid_hex24>``, globally unique. UUID-based PK
                    chosen over per-thread auto-increment because
                    composite FKs from ``agent_runs(turn_id, thread_id)``
                    need a globally unique id token (Codex CRITICAL
                    finding on earlier per-thread-seq draft).
  - turn_seq        Human-readable monotonic seq within a thread —
                    starts at 1, increments per ``open_turn``. Useful
                    in admin UIs and logs; NOT used as a key by the
                    application code.
  - thread_id       FK to agent_threads.
  - summary         Detereministic-template-generated summary written
                    after Stage 3 finalize (Group 5.3). Encrypted at the
                    application layer (``encrypt_value`` v2:) when it
                    can contain PII — column type is plain text here.
  - status          Enum: active / closed.
  - closed_at       NULL while active, set on close transition.
  - run_count,
    total_tokens,
    total_cost_usd  Closed-turn aggregates — survive 90-day
                    agent_runs retention (Group 6.6).

Constraints:
  - CHECK status IN ('active','closed')
  - CHECK consistency of status ↔ closed_at
  - UNIQUE (thread_id, turn_seq) — human-readable id within thread
  - UNIQUE (id, thread_id)       — needed for composite FK on agent_runs
  - Partial UNIQUE INDEX ON (thread_id) WHERE status='active' — at most
    one active turn per thread (defense in depth on top of per-thread
    FIFO queue + advisory_xact_lock).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class ConversationTurn(Base):
    """Per-thread thematic turn. See module docstring + Group 6.6."""

    __tablename__ = "conversation_turns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    turn_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_threads.id"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Deterministic-template summary, written once after Stage 3 "
            "finalize. May contain PII → encrypted at app layer "
            "(encrypt_value v2:) when appropriate. NULL until finalize."
        ),
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
    )
    # Closed-turn aggregates — Group 6.6. Carry forward token / cost
    # numbers past agent_runs' 90-day retention.
    run_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    total_cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4),
        nullable=False,
        default=0,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','closed')",
            name="ck_conversation_turns_status",
        ),
        CheckConstraint(
            "(status = 'active' AND closed_at IS NULL) "
            "OR (status = 'closed' AND closed_at IS NOT NULL)",
            name="ck_conversation_turns_status_closed_at",
        ),
        UniqueConstraint(
            "thread_id",
            "turn_seq",
            name="uq_conversation_turns_thread_seq",
        ),
        # Codex hollistic MINOR #10 — needed so agent_runs can declare a
        # composite FK on (turn_id, thread_id) that prevents a run from
        # pointing at a turn in a different thread.
        UniqueConstraint(
            "id",
            "thread_id",
            name="uq_conversation_turns_id_thread",
        ),
        # Partial unique index — at most one active turn per thread.
        # SQLAlchemy translates ``sqlite_where`` for SQLite tests; Postgres
        # uses ``postgresql_where``. The index name is referenced from
        # the conftest-built schema introspection tests.
        Index(
            "ix_one_active_turn_per_thread",
            "thread_id",
            unique=True,
            postgresql_where=sql_text("status = 'active'"),
            sqlite_where=sql_text("status = 'active'"),
        ),
        Index(
            "ix_conversation_turns_recent",
            "thread_id",
            "started_at",
        ),
    )
