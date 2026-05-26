"""conversation_turns + agent_runs.turn_id (Sub-A9, Group 6.6)

Adds the per-thread thematic-turn entity and wires it into the
existing agent_runs ledger via a composite FK.

Schema highlights (per Group 6.6):

  conversation_turns
    id                       text PK — turn_<uuid_hex24>, globally unique
    turn_seq                 bigint NOT NULL — human-readable seq within thread
    thread_id                FK → agent_threads.id
    tenant_id                text NOT NULL
    started_at               timestamptz NOT NULL
    closed_at                timestamptz NULL
    summary                  text NULL — deterministic-template summary
    status                   'active' | 'closed'
    run_count, total_tokens, total_cost_usd  closed-turn aggregates
    CHECK status IN ('active','closed')
    CHECK (active AND closed_at IS NULL) OR (closed AND closed_at IS NOT NULL)
    UNIQUE (thread_id, turn_seq)
    UNIQUE (id, thread_id)        — needed for composite FK from agent_runs
    UNIQUE INDEX ON (thread_id) WHERE status='active'   — at most one active per thread
    INDEX (thread_id, started_at)

  agent_runs
    ADD COLUMN turn_id text NULL
    ADD INDEX ix_agent_runs_turn_id (turn_id) WHERE turn_id IS NOT NULL
    ADD CONSTRAINT fk_agent_runs_turn_thread
      FOREIGN KEY (turn_id, thread_id)
      REFERENCES conversation_turns(id, thread_id)
      — defends against pointing at a turn in a different thread.

Group 6.6 backfill strategy A: legacy agent_runs rows leave turn_id
NULL forever. Planner-driven runs populate it from Stage 2 transition
(Category B). Read paths must tolerate NULL (Codex MAJOR #4).

Migration locking semantics (honest, not aspirational — Codex Sub-A9
R1 MAJOR #2): this revision uses the straightforward Alembic forms
(``op.add_column``, normal ``CREATE INDEX``, immediate ``ADD FOREIGN
KEY``). On Postgres that means a brief ACCESS EXCLUSIVE lock on
``agent_runs`` for the duration of the migration. Run during a
maintenance window or with a short statement_timeout to avoid
holding the lock under load. Online variants (``CREATE INDEX
CONCURRENTLY``, ``ADD CONSTRAINT … NOT VALID`` + ``VALIDATE
CONSTRAINT``) are documented in ``docs/runbooks/planner-mode.md``
(Phase E) when needed for zero-downtime rollouts.

SQLite compat (Codex Sub-A9 R1 MAJOR #1): adding a composite FK to
an existing table is not supported by SQLite's plain ``ALTER TABLE``.
We use ``op.batch_alter_table("agent_runs")`` so SQLAlchemy can rebuild
the table on SQLite while leaving Postgres' synchronous ALTER intact.

Revision ID: 20260526_0050
Revises: 20260526_0049
Create Date: 2026-05-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260526_0050"
down_revision = "20260526_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column(
            "thread_id",
            sa.String(64),
            sa.ForeignKey("agent_threads.id"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "total_cost_usd",
            sa.Numeric(10, 4),
            nullable=False,
            server_default="0",
        ),
        sa.CheckConstraint(
            "status IN ('active','closed')",
            name="ck_conversation_turns_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND closed_at IS NULL) "
            "OR (status = 'closed' AND closed_at IS NOT NULL)",
            name="ck_conversation_turns_status_closed_at",
        ),
        sa.UniqueConstraint(
            "thread_id",
            "turn_seq",
            name="uq_conversation_turns_thread_seq",
        ),
        sa.UniqueConstraint(
            "id",
            "thread_id",
            name="uq_conversation_turns_id_thread",
        ),
    )
    # Partial unique — at most one active turn per thread. Same
    # ``sqlite_where`` translation pattern as planner_gaps' open-index
    # so SQLite tests can exercise the constraint.
    op.create_index(
        "ix_one_active_turn_per_thread",
        "conversation_turns",
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_conversation_turns_recent",
        "conversation_turns",
        ["thread_id", "started_at"],
    )

    # agent_runs additions — Codex Sub-A9 R1 MAJOR #1: batch_alter_table
    # lets SQLite rebuild the table to add a composite FK that its
    # plain ALTER cannot do. On Postgres alembic skips the rebuild and
    # issues normal ALTER statements.
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(
            sa.Column("turn_id", sa.String(64), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_agent_runs_turn_thread",
            "conversation_turns",
            ["turn_id", "thread_id"],
            ["id", "thread_id"],
        )
        # Codex Sub-A9 R2 MAJOR #1 — UNIQUE(id, turn_id) target for
        # planner_executions composite FK below. ``id`` is already PK
        # so this adds no real uniqueness; it exists to satisfy
        # Postgres's FK-target requirement (composite FK must reference
        # a unique index, not just a PK on a sub-column).
        batch_op.create_unique_constraint(
            "uq_agent_runs_id_turn",
            ["id", "turn_id"],
        )
    op.create_index(
        "ix_agent_runs_turn_id",
        "agent_runs",
        ["turn_id"],
        postgresql_where=sa.text("turn_id IS NOT NULL"),
        sqlite_where=sa.text("turn_id IS NOT NULL"),
    )

    # Codex Sub-A9 R1 MAJOR #4 + R2 MAJOR #1 — planner_executions.turn_id
    # uses a COMPOSITE FK (run_id, turn_id) → agent_runs(id, turn_id).
    # This simultaneously enforces:
    #   - referential integrity on turn_id (via transitive chain through
    #     agent_runs' own composite FK to conversation_turns).
    #   - consistency between planner_executions.turn_id and agent_runs.turn_id
    #     for the same run_id — no more "run_A but turn_B" drift.
    # The simple FK on planner_executions.run_id → agent_runs.id was
    # created in migration 0049 and remains; it covers the case where
    # turn_id is NULL (composite FK is inert with NULL columns).
    with op.batch_alter_table("planner_executions") as batch_op:
        batch_op.create_foreign_key(
            "fk_planner_executions_run_turn",
            "agent_runs",
            ["run_id", "turn_id"],
            ["id", "turn_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("planner_executions") as batch_op:
        batch_op.drop_constraint(
            "fk_planner_executions_run_turn",
            type_="foreignkey",
        )
    op.drop_index("ix_agent_runs_turn_id", table_name="agent_runs")
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_constraint(
            "uq_agent_runs_id_turn",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_agent_runs_turn_thread",
            type_="foreignkey",
        )
        batch_op.drop_column("turn_id")

    op.drop_index(
        "ix_conversation_turns_recent", table_name="conversation_turns"
    )
    op.drop_index(
        "ix_one_active_turn_per_thread", table_name="conversation_turns"
    )
    op.drop_table("conversation_turns")
