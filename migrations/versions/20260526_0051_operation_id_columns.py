"""operation_id + normalized_title_hash on user-facing tables (Sub-A10, Group 3.1)

Adds two idempotency-supporting columns to the five user-facing tables
the planner-flow will populate via tool calls:

  - shopping_list_items
  - family_reminders
  - tasks
  - recipes
  - checklists

Columns:

  operation_id text NULL
      SHA-1 hex of (plan_id, step_id, action, entity_type, logical_key)
      for ``create`` operations, or (plan_id, step_id, action,
      entity_type, entity_id) for ``update`` / ``delete``. Lets a
      retry of the same plan-step against the same canonical title
      produce the same op_id; partial unique index on
      ``(tenant_id, operation_id)`` makes ``INSERT ... ON CONFLICT
      DO NOTHING`` idempotent.

  normalized_title_hash text NULL
      SHA-256 hex of ``normalize_for_dedup(title)`` (Russian
      lemmatization via pymorphy3). Indexed for fast semantic-dedup
      lookups. We store a HASH rather than the lemma plaintext so
      indexes don't leak content from tables where ``title`` is
      encrypted at rest via ``EncryptedString``.

Indexes:

  ix_<table>_operation_id     UNIQUE (tenant_id, operation_id)
                              WHERE operation_id IS NOT NULL
                              — idempotent-write target.
  ix_<table>_normalized_title (tenant_id, normalized_title_hash)
                              WHERE normalized_title_hash IS NOT NULL
                              — semantic-dedup lookup.

Both indexes are partial-on-NOT-NULL so legacy rows (which leave
both columns NULL) don't compete with new ones.

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
        op.create_index(
            f"ix_{table}_operation_id",
            table,
            ["tenant_id", "operation_id"],
            unique=True,
            postgresql_where=sa.text("operation_id IS NOT NULL"),
            sqlite_where=sa.text("operation_id IS NOT NULL"),
        )
        op.create_index(
            f"ix_{table}_normalized_title",
            table,
            ["tenant_id", "normalized_title_hash"],
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
