"""add eds claim state history fields

Revision ID: 20260324_0002
Revises: 20260323_0001
Create Date: 2026-03-24 15:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260324_0002"
down_revision = "20260323_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "eds_claim_state",
        sa.Column("last_seen_changed", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "eds_claim_state",
        sa.Column("last_history_order", sa.Integer(), nullable=True),
    )
    op.add_column(
        "eds_claim_state",
        sa.Column("last_history_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "eds_claim_state",
        sa.Column("last_history_date", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "eds_claim_state",
        sa.Column("last_notified_event_key", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("eds_claim_state", "last_notified_event_key")
    op.drop_column("eds_claim_state", "last_history_date")
    op.drop_column("eds_claim_state", "last_history_code")
    op.drop_column("eds_claim_state", "last_history_order")
    op.drop_column("eds_claim_state", "last_seen_changed")
