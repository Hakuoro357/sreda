"""add family_members table + nutrition columns on recipes

Housewife v1.2 scaffold — two schema changes in one migration because
they ship together:

  * ``family_members`` — structured member records per (tenant, user).
    Replaces the loose "состав семьи" free-form memory with editable
    rows (name, role, birth_year, notes). Powers shopping-list scaling
    and LLM context enrichment at menu planning time.
  * ``recipes.calories_per_serving`` + three BJU columns (protein /
    fat / carbs). All nullable floats — LLM fills when saving, display
    layer skips when absent.

No data migration: existing recipes get NULL nutrition (user can
re-save or let background enrichment fill later).

Revision ID: 20260421_0020
Revises: 20260421_0019
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260421_0020"
down_revision = "20260421_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- family_members ----------
    op.create_table(
        "family_members",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # Encrypted at ORM layer. Names are identifying data.
        sa.Column("name", sa.Text(), nullable=False),
        # self | spouse | child | parent | other
        sa.Column("role", sa.String(length=16), nullable=False),
        # birth year preferred (stable). age_hint is fallback string
        # for cases where user said "8 лет" without birth year.
        sa.Column("birth_year", sa.Integer(), nullable=True),
        sa.Column("age_hint", sa.String(length=64), nullable=True),
        # Encrypted. Free-form: "аллергия на горчицу", "вегетарианка".
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_family_members_tenant_user",
        "family_members",
        ["tenant_id", "user_id"],
    )

    # ---------- recipes: nutrition columns ----------
    with op.batch_alter_table("recipes") as batch:
        batch.add_column(
            sa.Column("calories_per_serving", sa.Float(), nullable=True)
        )
        batch.add_column(
            sa.Column("protein_per_serving", sa.Float(), nullable=True)
        )
        batch.add_column(
            sa.Column("fat_per_serving", sa.Float(), nullable=True)
        )
        batch.add_column(
            sa.Column("carbs_per_serving", sa.Float(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("recipes") as batch:
        batch.drop_column("carbs_per_serving")
        batch.drop_column("fat_per_serving")
        batch.drop_column("protein_per_serving")
        batch.drop_column("calories_per_serving")

    op.drop_index("ix_family_members_tenant_user", table_name="family_members")
    op.drop_table("family_members")
