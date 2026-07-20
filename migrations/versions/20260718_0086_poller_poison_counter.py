"""poller_offsets: durable poison-счётчик (C6/M14, R1 audit 2026-07-18)

Долговременный счётчик «ядовитых» inbound-апдейтов long-poll'а НА СУЩЕСТВУЮЩЕЙ
таблице ``poller_offsets`` (одна строка на канал). Раньше счётчик жил ТОЛЬКО
в памяти процесса:

- M14: рестарт обнулял счётчик → детерминированно роняющий хендлер апдейт
  мог никогда не достичь потолка попыток и вечно блокировать голову очереди
  (head-of-line), heartbeat при этом зелёный.
- C6: dead-letter двигал offset БЕЗ durable-сохранения апдейта → безвозвратная
  потеря (поллер логирует «мёртвый» апдейт в файл-журнал ПЕРЕД сдвигом —
  восстановимо вручную; отдельного шифрованного хранилища НЕ вводим —
  пропорционально).

Две nullable/default колонки, никаких новых таблиц:
- ``poison_update_id`` BIGINT NULL — update_id текущего сбоящего апдейта.
- ``poison_count``    INT NOT NULL DEFAULT 0 — число ПОДРЯД идущих неудач.

Success/dead-letter сбрасывают их в NULL/0.

Revision ID: 20260718_0086
Revises: 20260718_0085
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260718_0086"
down_revision = "20260718_0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("poller_offsets") as b:
        b.add_column(
            sa.Column("poison_update_id", sa.BigInteger(), nullable=True)
        )
        b.add_column(
            sa.Column(
                "poison_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("poller_offsets") as b:
        b.drop_column("poison_count")
        b.drop_column("poison_update_id")
