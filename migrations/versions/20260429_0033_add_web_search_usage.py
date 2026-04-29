"""add web_search_usage counter table

Контекст: web_search через DuckDuckGo Search SERP (`bing.com` под
капотом) ломается с RU egress (Bing 403). Переезжаем на Tavily API
(1000 query/мес free tier на key). Для распределения этой квоты по
юзерам + контроля общего расхода нужен per-(tenant, user, month)
счётчик.

Таблица — точная копия паттерна `free_tier_usage` (UNIQUE на тройку
для UPSERT-pattern, read-modify-write в `WebSearchUsageCounter`).

Поля:
* `tavily_calls` — счёт основных запросов (которые попали в Tavily)
* `fallback_calls` — счёт DDG-fallback'ов (когда юзер исчерпал квоту
  и tool fall'нулся на DDG `backend="api"`). Считаем отдельно чтобы
  в админке видеть конверсию quota → fallback.

Revision ID: 20260429_0033
Revises: 20260428_0032
Create Date: 2026-04-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260429_0033"
down_revision = "20260428_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_search_usage",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("year_month", sa.String(7), nullable=False, index=True),
        sa.Column("tavily_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_web_search_usage_unique",
        "web_search_usage",
        ["tenant_id", "user_id", "year_month"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_web_search_usage_unique", table_name="web_search_usage")
    op.drop_table("web_search_usage")
