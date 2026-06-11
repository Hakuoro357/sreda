"""#127: индекс inbound_messages.created_at для чанковой ретеншн-чистки.

Чистка ретеншна (retention_cleanup) выбирает устаревшие входящие
порциями с фильтром ``created_at < cutoff`` + ``ORDER BY created_at, id``.
Без индекса каждый чанк — полный скан таблицы; на бэклоге после простоя
это N сканов подряд (Codex R2 high, #127).

Additive-only: один индекс, данные не трогаются.
"""

from alembic import op


revision = "20260611_0055"
down_revision = "ea946de2b484"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_inbound_messages_created_at",
        "inbound_messages",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbound_messages_created_at", table_name="inbound_messages")
