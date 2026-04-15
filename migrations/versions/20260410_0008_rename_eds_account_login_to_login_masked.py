"""rename eds_accounts.login to login_masked (M5)

Raw login must not be stored in the operational table per spec 31.
The column is renamed to ``login_masked`` and all write sites now
store the masked version instead of the plaintext login.

Revision ID: 20260410_0008
Revises: 20260328_0007
Create Date: 2026-04-10 18:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260410_0008"
down_revision = "20260328_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("eds_accounts") as batch_op:
        batch_op.alter_column("login", new_column_name="login_masked")


def downgrade() -> None:
    with op.batch_alter_table("eds_accounts") as batch_op:
        batch_op.alter_column("login_masked", new_column_name="login")
