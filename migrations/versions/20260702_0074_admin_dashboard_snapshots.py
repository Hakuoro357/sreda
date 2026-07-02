"""admin_dashboard_snapshots — снапшот обзорного дашборда админки (#292)

Key-value таблица: фоновый рефреш в job_runner пишет JSON-агрегаты (балансы
провайдеров, расход-$ по моделям, ошибки/медленные за 24ч), страница /admin
только читает одну строку — на открытии ноль сетевых вызовов и тяжёлых
запросов (требование владельца 2026-07-02). CREATE TABLE новой таблицы — без
блокировок существующих данных, мгновенно. Образец — 0073 fetch_url_usage.

Revision ID: 20260702_0074
Revises: 20260630_0073
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260702_0074"
down_revision = "20260630_0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_dashboard_snapshots",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("admin_dashboard_snapshots")
