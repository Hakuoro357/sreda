"""add proactive decision fields (Phase 5-lite)

Infrastructure for rules-based ``decide_to_speak`` between skill's
proactive handler and outbox:

  * ``tenant_user_profiles.proactive_throttle_minutes`` — per-user cap
    on how often a skill may push proactive messages. Default 30
    minutes; ``/throttle <N>`` sets it.
  * ``outbox_messages.drop_reason`` — explains why a message was
    dropped (duplicate / throttle / LLM-filter / mute). Powers the
    ``/stats`` surface.
  * ``outbox_messages.status`` gains value ``"dropped"`` (written in
    application layer; no schema constraint here — column is already
    free-form String).

Revision ID: 20260415_0016
Revises: 20260415_0015
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0016"
down_revision = "20260415_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_user_profiles") as batch_op:
        batch_op.add_column(
            sa.Column(
                "proactive_throttle_minutes",
                sa.Integer(),
                nullable=False,
                server_default="30",
            )
        )
    with op.batch_alter_table("tenant_user_profiles") as batch_op:
        batch_op.alter_column("proactive_throttle_minutes", server_default=None)

    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.add_column(
            sa.Column("drop_reason", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            "ix_outbox_messages_drop_reason", ["drop_reason"]
        )


def downgrade() -> None:
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.drop_index("ix_outbox_messages_drop_reason")
        batch_op.drop_column("drop_reason")
    with op.batch_alter_table("tenant_user_profiles") as batch_op:
        batch_op.drop_column("proactive_throttle_minutes")
