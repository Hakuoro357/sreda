"""add reminder escalation columns

Part of the reminder-escalation feature (v1.2). Each firing of a
one-shot reminder now sends inline buttons "Сделал ✅ / Отложить ⏰".
If neither is tapped within 2 minutes the worker re-fires once;
after that the reminder is finalised regardless.

Two new nullable columns on ``family_reminders``:

  * ``acknowledged_at`` — set when the user taps "Сделал". Distinguishes
    a cleanly-closed reminder (user saw it) from one that timed out
    without any interaction.
  * ``escalation_count`` — integer, default 0. Incremented each time
    the worker fires this reminder instance. The worker caps at a
    module constant (currently 2 = "original message + 1 re-ping")
    and then finalises via the usual rrule/fired path.

Both columns are additive — existing rows continue to behave as today
(escalation_count=0, no ack required; caller logic uses the new state
machine).

Revision ID: 20260421_0021
Revises: 20260421_0020
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260421_0021"
down_revision = "20260421_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("family_reminders") as batch:
        batch.add_column(
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "escalation_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("family_reminders") as batch:
        batch.drop_column("escalation_count")
        batch.drop_column("acknowledged_at")
