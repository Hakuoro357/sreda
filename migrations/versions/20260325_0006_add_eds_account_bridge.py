"""add eds account bridge to tenant eds accounts

Revision ID: 20260325_0006
Revises: 20260325_0005
Create Date: 2026-03-25 23:35:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260325_0006"
down_revision = "20260325_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "eds_accounts",
        sa.Column("tenant_eds_account_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_eds_accounts_tenant_eds_account_id",
        "eds_accounts",
        ["tenant_eds_account_id"],
        unique=True,
    )
    op.create_foreign_key(
        "fk_eds_accounts_tenant_eds_account_id",
        "eds_accounts",
        "tenant_eds_accounts",
        ["tenant_eds_account_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_eds_accounts_tenant_eds_account_id", "eds_accounts", type_="foreignkey")
    op.drop_index("ix_eds_accounts_tenant_eds_account_id", table_name="eds_accounts")
    op.drop_column("eds_accounts", "tenant_eds_account_id")
