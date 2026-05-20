"""runtime_config.value: widen to Text for structured toggles

Tenant-level LLM canary routing stores a compact JSON object in
``runtime_config.chat_provider_tenant_overrides_json``. A few tenants can fit
inside varchar(256), but this key is an operator-facing runtime toggle and
should not fail when the canary list grows.

Revision ID: 20260520_0047
Revises: 20260518_0046
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260520_0047"
down_revision = "20260518_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_config") as batch_op:
        batch_op.alter_column(
            "value",
            existing_type=sa.String(length=256),
            type_=sa.Text(),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("runtime_config") as batch_op:
        batch_op.alter_column(
            "value",
            existing_type=sa.Text(),
            type_=sa.String(length=256),
            nullable=True,
        )
