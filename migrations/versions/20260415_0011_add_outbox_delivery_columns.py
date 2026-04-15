"""add outbox_messages.user_id + is_interactive (Phase 2d)

Adds the two columns the ``OutboxDeliveryWorker`` needs:

  * ``user_id``         — so the delivery worker can resolve per-user
    profile + per-skill notification priority without joining through
    ``agent_runs`` → ``agent_threads`` → ``users``.
  * ``is_interactive``  — shortcut flag set by ``node_persist_replies``
    when the graph invocation was triggered by an inbound user message.
    Interactive deliveries bypass quiet-hours (users always get a reply
    to their own command), so the worker can skip the policy lookup
    entirely for those rows.

Revision ID: 20260415_0011
Revises: 20260415_0010
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0011"
down_revision = "20260415_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "user_id",
                sa.String(length=64),
                sa.ForeignKey("users.id"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_interactive",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index("ix_outbox_messages_user_id", ["user_id"])

    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.alter_column("is_interactive", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.drop_index("ix_outbox_messages_user_id")
        batch_op.drop_column("is_interactive")
        batch_op.drop_column("user_id")
