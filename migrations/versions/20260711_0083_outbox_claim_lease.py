"""outbox_messages: +claim_token +lease_expires_at (atomic-claim lease, #344 F5)

Аудит #336 F5: outbox-доставка без claim → два воркера выбирают ту же pending-
строку и шлют дважды. Фикс — атомарный claim с lease, ВЫРАЖЕННЫЙ отдельными
nullable-полями (НЕ новым значением ``status``): строка остаётся ``pending`` во
время lease, claim = ``claim_token`` + ``lease_expires_at``.

Backwards-compatible / rollback-safe (§4 валидации):
- Оба поля nullable, без backfill — существующие строки получают NULL (claimable).
  add_column nullable без default = metadata-only, без переписывания таблицы.
- СТАРЫЙ воркер (после отката) эти поля НЕ читает, выбирает по ``status='pending'``
  и доставляет как раньше (at-least-once) → откат не застревает.
- Частичный индекс на ``lease_expires_at`` (только non-NULL И status='pending')
  ускоряет скан claimable-строк без раздувания индекса.

Индекс строится ОБЫЧНЫМ transactional ``CREATE INDEX`` (в теле миграции), НЕ
CONCURRENTLY (#344 R7, решение владельца). Обоснование: обычный CREATE INDEX берёт
SHARE-лок на ``outbox_messages`` на время построения — но замер прода 2026-07-11
показал таблицу ~604 строки / ~5 MB (delivery-очередь, строки терминализируются
sent/failed/dropped), где построение индекса — миллисекунды и лок negligible.
CONCURRENTLY (рассматривался на R6) НЕЛЬЗЯ внутри транзакции → его вынос в
``autocommit_block`` РАЗРЫВАЛ атомарность миграции: колонки коммитились ДО индекса,
а ревизия стемпилась только на успешном возврате ``upgrade()`` → сбой concurrent-build
оставлял схему БЕЗ записи ревизии, повтор ``alembic upgrade`` падал на дублирующем
``add_column`` (Codex sol+terra R6 MAJOR — recoverability). Transactional CREATE INDEX
атомарен с revision-стемпом → сбой откатывается чисто, повтор безопасен. Для маленькой
таблицы это строго лучший трейд. SQLite (тесты) — тот же обычный CREATE INDEX.

Revision ID: 20260711_0083
Revises: 20260709_0082
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260711_0083"
down_revision = "20260709_0082"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_outbox_lease_expires_at"
_INDEX_WHERE = "lease_expires_at IS NOT NULL AND status = 'pending'"


def upgrade() -> None:
    op.add_column(
        "outbox_messages",
        sa.Column("claim_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "outbox_messages",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Обычный transactional CREATE INDEX (НЕ CONCURRENTLY) — атомарен с
    # revision-стемпом, recoverable; таблица мала → лок negligible (см. docstring).
    op.create_index(
        _INDEX_NAME,
        "outbox_messages",
        ["lease_expires_at"],
        postgresql_where=sa.text(_INDEX_WHERE),
        sqlite_where=sa.text(_INDEX_WHERE),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="outbox_messages")
    op.drop_column("outbox_messages", "lease_expires_at")
    op.drop_column("outbox_messages", "claim_token")
