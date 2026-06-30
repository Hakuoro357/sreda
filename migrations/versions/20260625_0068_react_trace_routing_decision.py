"""routing_decision_json on react_turn_trace (#221 Ф3b — лог решения доменного роутера)

Доменный роутер (#221) на shadow/execute пишет своё решение (БЕЗ ПД — только имена доменов/семей +
confidence + флаги) в новую колонку трейса хода. Источник для измерения shadow-расхождений (≤5% порог
перехода shadow→execute) и задел под будущую петлю самообучения. NULL в disabled-режиме (никаких новых
данных при выключенном роутере).

Безопасность миграции: ЧИСТО АДДИТИВНАЯ nullable-колонка, без индекса и без бэкфилла. ALTER ... ADD COLUMN
nullable в Postgres — метаданные, мгновенно, без переписи таблицы и без блокировки чтения/записи (даже на
большой react_turn_trace). batch_alter_table — для SQLite. PG-lock_timeout для подстраховки.

Revision ID: 20260625_0068
Revises: 20260623_0067
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260625_0068"
down_revision = "20260623_0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Подстраховка: не зависнуть на блокировке (ADD COLUMN nullable — метаданные, отработает мгновенно).
        op.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
    with op.batch_alter_table("react_turn_trace") as batch_op:
        batch_op.add_column(sa.Column("routing_decision_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("react_turn_trace") as batch_op:
        batch_op.drop_column("routing_decision_json")
