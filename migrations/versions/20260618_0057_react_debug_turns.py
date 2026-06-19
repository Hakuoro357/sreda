"""vex#170: временная debug-таблица ходов ReAct — react_debug_turns.

Аддитивная миграция: одна таблица + 2 индекса. ПД (текст вопроса/ответа) шифруется на
уровне приложения (EncryptedString → хранится как Text). Временная: после тестирования
нового механизма удаляется отдельной drop-миграцией. Запись гейтится allowlist'ом тенантов
SREDA_REACT_DEBUG_TENANTS (дефолт пуст → никому).
"""

import sqlalchemy as sa
from alembic import op

revision = "20260618_0057"
down_revision = "20260612_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_react_debug_created", table_name="react_debug_turns")
    op.drop_index("ix_react_debug_tenant_created", table_name="react_debug_turns")
    op.drop_table("react_debug_turns")
