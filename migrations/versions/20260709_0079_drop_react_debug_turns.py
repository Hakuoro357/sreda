"""#138 Ф3-0: дроп временной таблицы react_debug_turns (vex#170/#185).

Owner-решение 2026-07-06 (Ф0 §5.4): временный QA-захват переписки полностью заменён
durable-трейсом react_turn_trace (#192, тот же контент EncryptedString + структура);
на проде запись в react_debug_turns давно short-circuit'ится флагом react_trace_enabled=1
(dual-write guard) — живого writer'а нет. Дропаем ДО включения RLS (Ф3), чтобы не
денормализовать/не RLS-ить мёртвую таблицу с ПД.

Данные НЕ переносим (QA-захват, TTL 14д, ретеншн и так стриг; актуальный след — в
react_turn_trace). Модель ReactDebugTurn + флаги SREDA_REACT_DEBUG_TENANTS /
SREDA_REACT_DEBUG_ALL + ветка ретеншна вырезаны тем же коммитом.

downgrade: воссоздаёт пустую таблицу по модели на момент удаления (данные не возвращаются).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260709_0079"
down_revision = "20260706_0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF EXISTS — идемпотентно (свежие dev-БД, где модель уже убрана из Base.metadata,
    # таблицы не имеют). Работает и на PG, и на SQLite.
    op.execute("DROP TABLE IF EXISTS react_debug_turns")


def downgrade() -> None:
    # Пустая таблица ТОЧНО по схеме миграции 20260618_0057 (данные не восстанавливаются).
    op.create_table(
        "react_debug_turns",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("thread_id", sa.String(128), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False, server_default="react"),
        sa.Column("kind", sa.String(16), nullable=False, server_default="final"),
        sa.Column("user_text", sa.Text(), nullable=True),
        sa.Column("reply_text", sa.Text(), nullable=True),
        sa.Column("tools_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_react_debug_tenant_created", "react_debug_turns",
                    ["tenant_id", "created_at"])
    op.create_index("ix_react_debug_created", "react_debug_turns", ["created_at"])
