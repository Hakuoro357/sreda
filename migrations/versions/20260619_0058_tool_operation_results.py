"""vex#163 Фаза 1в: субстрат прод-идемпотентности — таблица tool_operation_results.

Аддитивная миграция: одна НОВАЯ таблица (без коллизии с прод-данными) + unique(operation_id) +
scope-индекс. Хранит результат durable-операции для exact-replay (повтор того же operation_id →
сохранённый payload без повторной мутации) и tombstone (hard-delete replay «уже сделано»). ПД
(stable_return_payload) шифруется на уровне приложения (JSONEncryptedString → Text). Гейтится
per-tenant флагом субстрата (дефолт ВЫКЛ); строки появляются только с момента включения (backfill
не делаем — план #163).
"""

import sqlalchemy as sa
from alembic import op

revision = "20260619_0058"
down_revision = "20260618_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_operation_results",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("operation_family", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=True),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("args_hmac", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("stable_return_payload", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tombstone_until", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("operation_id", name="uq_tool_operation_results_operation_id"),
    )
    op.create_index("ix_tool_operation_results_scope", "tool_operation_results",
                    ["tenant_id", "user_id", "operation_family"])


def downgrade() -> None:
    op.drop_index("ix_tool_operation_results_scope", table_name="tool_operation_results")
    op.drop_table("tool_operation_results")
