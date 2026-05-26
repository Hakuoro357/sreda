"""operation_id + normalized_title_hash on user-facing tables (Sub-A10, Group 3.1)

Adds two idempotency-supporting columns to the five user-facing tables
the planner-flow will populate via tool calls:

  - shopping_list_items
  - family_reminders
  - tasks_items
  - recipes
  - checklists

Columns:

  operation_id text NULL
      SHA-1 hex of (plan_id, step_id, action, entity_type, logical_key)
      for ``create`` operations, or (plan_id, step_id, action,
      entity_type, entity_id) for ``update`` / ``delete``. Lets a
      retry of the same plan-step against the same canonical title
      produce the same op_id; unique index on
      ``(tenant_id, user_id, operation_id)`` makes
      ``INSERT ... ON CONFLICT DO NOTHING`` idempotent.

  normalized_title_hash text NULL
      HMAC-SHA256 hex of ``normalize_for_dedup(title)`` keyed by
      ``SREDA_ENCRYPTION_KEY`` (Russian lemmatization via pymorphy3).
      Indexed for fast semantic-dedup lookups. We use HMAC rather
      than plain SHA-256 because shopping titles like "молоко" /
      "хлеб" are low-entropy and a plain hash is dictionary-attackable
      — HMAC with the server secret leaves the index resistant to
      offline rainbow tables. Codex Sub-A10 R1 MAJOR #4.

Indexes (Codex Sub-A10 R1 CRITICAL #1 + MAJOR #3):

  ix_<table>_operation_id      UNIQUE (tenant_id, user_id,
                               operation_id) — full (not partial)
                               unique so ``ON CONFLICT`` works
                               without an ``index_where`` clause.
                               NULL values don't collide in standard
                               UNIQUE (Postgres + SQLite both treat
                               NULL as distinct).
  ix_<table>_normalized_title  (tenant_id, user_id,
                               normalized_title_hash) — semantic-
                               dedup lookup. Non-unique.

Both indexes are now scoped (tenant_id, user_id, ...) so two users
in the same tenant don't collide on the same shopping item name.

Group 6.6 migration-safety notes apply identically here (see comment
block in 0050): straightforward synchronous ALTER on Postgres,
``batch_alter_table`` on SQLite. Production rollout under maintenance
window.

Revision ID: 20260526_0051
Revises: 20260526_0050
Create Date: 2026-05-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260526_0051"
down_revision = "20260526_0050"
branch_labels = None
depends_on = None


# Tables that get the same two columns + same two index patterns.
_TARGET_TABLES = (
    "shopping_list_items",
    "family_reminders",
    "tasks_items",         # ORM class Task — table name "tasks_items"
    "recipes",
    "checklists",
)


def upgrade() -> None:
    for table in _TARGET_TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column("operation_id", sa.String(64), nullable=True)
            )
            batch_op.add_column(
                sa.Column(
                    "normalized_title_hash", sa.String(64), nullable=True
                )
            )
        # Codex Sub-A10 R1 CRITICAL #1 — plain UNIQUE, NOT partial.
        # ON CONFLICT (tenant_id, user_id, operation_id) DO NOTHING
        # matches a full unique without needing an index_where clause.
        # NULL operation_id (legacy) doesn't collide because standard
        # SQL treats each NULL as distinct in UNIQUE constraints.
        # Codex Sub-A10 R1 MAJOR #3 — scope includes user_id so two
        # users in one tenant don't collide on the same item name.
        op.create_index(
            f"ix_{table}_operation_id",
            table,
            ["tenant_id", "user_id", "operation_id"],
            unique=True,
        )
        op.create_index(
            f"ix_{table}_normalized_title",
            table,
            ["tenant_id", "user_id", "normalized_title_hash"],
            postgresql_where=sa.text("normalized_title_hash IS NOT NULL"),
            sqlite_where=sa.text("normalized_title_hash IS NOT NULL"),
        )


def downgrade() -> None:
    for table in reversed(_TARGET_TABLES):
        op.drop_index(f"ix_{table}_normalized_title", table_name=table)
        op.drop_index(f"ix_{table}_operation_id", table_name=table)
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("normalized_title_hash")
            batch_op.drop_column("operation_id")
