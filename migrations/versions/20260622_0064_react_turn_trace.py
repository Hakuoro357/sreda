"""#192 — durable структурный трейс хода ReAct (react_turn_trace).

Одна строка на ход (вопрос→граф→ответ): идентичность (tenant/user/thread/channel/turn_key), статус
жизненного цикла (in_progress|awaiting_confirm|done), контент (зашифрован), наблюдательная структура
(llm_calls/tool_calls/outcome/passes). Заменяет временный react_debug_turns (#185) — drop старой
таблицы ОТДЕЛЬНОЙ миграцией позже (после проверки на проде). Аддитивно, БД не ломаем.

Expression-unique uq_react_turn_trace_scope (tenant_id, coalesce(user_id,''), turn_key): обычный
UNIQUE в PG допускает несколько NULL → tenant-wide (user_id=NULL) двоился бы; coalesce снимает.
ON CONFLICT в коде таргетит ТЕМ ЖЕ выражением (см. план #192).

Revision ID: 20260622_0064
Revises: 20260622_0063  (перецеплено при синхроне с origin/main: их 0063 = merge_housewife_free_plans #200)
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260622_0064"
down_revision = "20260622_0063"
branch_labels = None
depends_on = None

_TABLE = "react_turn_trace"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("thread_id", sa.String(128), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False, server_default="react"),
        sa.Column("turn_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="in_progress"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin_user_text", sa.Text(), nullable=True),  # EncryptedString → Text
        sa.Column("reply_text", sa.Text(), nullable=True),
        sa.Column("llm_calls_json", sa.Text(), nullable=True),
        sa.Column("tool_calls_json", sa.Text(), nullable=True),
        sa.Column("confirm_state", sa.String(16), nullable=False, server_default="none"),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("passes", sa.Integer(), nullable=False, server_default="0"),
    )
    # R3/R4: expression-unique по coalesce(user_id,'') — иначе tenant-wide (NULL) двоится.
    op.create_index(
        "uq_react_turn_trace_scope",
        _TABLE,
        ["tenant_id", sa.text("coalesce(user_id, '')"), "turn_key"],
        unique=True,
    )
    op.create_index(
        "ix_react_trace_tenant_user_created", _TABLE, ["tenant_id", "user_id", "created_at"],
    )
    op.create_index("ix_react_trace_turn_key", _TABLE, ["turn_key"])
    op.create_index("ix_react_trace_outcome", _TABLE, ["outcome"])
    op.create_index("ix_react_trace_status_created", _TABLE, ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_react_trace_status_created", table_name=_TABLE)
    op.drop_index("ix_react_trace_outcome", table_name=_TABLE)
    op.drop_index("ix_react_trace_turn_key", table_name=_TABLE)
    op.drop_index("ix_react_trace_tenant_user_created", table_name=_TABLE)
    op.drop_index("uq_react_turn_trace_scope", table_name=_TABLE)
    op.drop_table(_TABLE)
