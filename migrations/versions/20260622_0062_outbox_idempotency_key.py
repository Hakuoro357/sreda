"""#163 Фаза 4 — outbox_messages.idempotency_key + partial-unique индекс (дедуп доставки).

Ключ идемпотентности доставки С КАНАЛОМ+ботом: не более ОДНОГО outbox-сообщения на
(источник + канал + бот). Воркер напоминаний кладёт ``{reminder_id}:{fired_trigger_iso}:{channel}:
{bot_key}`` — эскалационные ре-пинги (разный fired_trigger) и dual TG+MAX (разный канал) РАЗЛИЧАЮТСЯ;
повтор той же тройки (мультипроцесс / повтор-enqueue) дедупится. Партиал ``WHERE idempotency_key IS
NOT NULL``: прочие продюсеры (без ключа) НЕ ограничены — аддитивно, легаси не схлопывается.

Дёшево даже на большой outbox_messages: колонка nullable = метаданные (instant, без переписи), а на
момент миграции ВСЕ строки имеют key=NULL → партиал-индекс пуст → build мгновенный, долгий лок не нужен.
Имя индекса = модель (uq_outbox_idempotency_key). Downgrade — drop индекса + колонки.

Revision ID: 20260622_0062
Revises: 20260622_0061
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260622_0062"
down_revision = "20260622_0061"
branch_labels = None
depends_on = None

_IDX = "uq_outbox_idempotency_key"


def upgrade() -> None:
    op.add_column(
        "outbox_messages",
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
    )
    op.create_index(
        _IDX,
        "outbox_messages",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_IDX, table_name="outbox_messages")
    op.drop_column("outbox_messages", "idempotency_key")
