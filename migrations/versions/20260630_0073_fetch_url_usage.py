"""fetch_url_usage — per-(tenant,user,day) счётчик fetch_url (#244 квота, no-refund)

Новая таблица анти-злоупотребления для публичного fetch_url. CREATE TABLE новой таблицы — без блокировок
существующих данных, мгновенно. Композитный UNIQUE(tenant_id,user_id,ymd) обязателен для атомарного
INSERT…ON CONFLICT в try_consume_fetch_url. Образец — 0033 web_search_usage / 0069 react_summaries.

Revision ID: 20260630_0073
Revises: 20260630_0072
Create Date: 2026-06-30

NB: ПЕРЕЦЕПЛЕНО 0069→0072 при синке ветки с main перед деплоем #244. main принёс #262
(20260630_0070_memory_categories → 0071 → 0072) от того же предка 0069 → revision id «20260630_0070»
КОЛЛИДИРОВАЛ с моим. Линеаризация: …0069 → 0070/0071/0072 (#262 память) → 0073 (#244 fetch_url_usage).
Проверено `alembic heads` = одна голова 20260630_0073.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260630_0073"
down_revision = "20260630_0072"
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
