"""fetch_url_usage — per-(tenant,user,day) счётчик fetch_url (#244 квота, no-refund)

Новая таблица анти-злоупотребления для публичного fetch_url. CREATE TABLE новой таблицы — без блокировок
существующих данных, мгновенно. Композитный UNIQUE(tenant_id,user_id,ymd) обязателен для атомарного
INSERT…ON CONFLICT в try_consume_fetch_url. Образец — 0033 web_search_usage / 0069 react_summaries.

Revision ID: 20260630_0070
Revises: 20260629_0069
Create Date: 2026-06-30

NB: голова на момент написания — 20260629_0069 (react_summaries, #232), пришла с синком ветки на main
ПЕРЕД началом #244. down_revision перецеплен 0068→0069 соответственно (проверено `alembic heads` = одна голова).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260630_0070"
down_revision = "20260629_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fetch_url_usage",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("ymd", sa.String(10), nullable=False),  # YYYY-MM-DD UTC
        sa.Column("fetch_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_fetch_url_usage_tenant_id", "fetch_url_usage", ["tenant_id"])
    op.create_index("ix_fetch_url_usage_user_id", "fetch_url_usage", ["user_id"])
    op.create_index("ix_fetch_url_usage_ymd", "fetch_url_usage", ["ymd"])
    op.create_index(
        "ix_fetch_url_usage_unique", "fetch_url_usage",
        ["tenant_id", "user_id", "ymd"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_fetch_url_usage_unique", table_name="fetch_url_usage")
    op.drop_index("ix_fetch_url_usage_ymd", table_name="fetch_url_usage")
    op.drop_index("ix_fetch_url_usage_user_id", table_name="fetch_url_usage")
    op.drop_index("ix_fetch_url_usage_tenant_id", table_name="fetch_url_usage")
    op.drop_table("fetch_url_usage")
