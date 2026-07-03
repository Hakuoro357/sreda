"""react_turn_trace: +turn_policy_json, +confirm_resolution (#285 Фаза A)

Две nullable-колонки наблюдаемости единого пути (shadow):
- turn_policy_json TEXT — снапшот TurnPolicy хода (сайдкар, БЕЗ ПД); NULL при выключенном
  флаге SREDA_REACT_UNIFIED_PATH_ENABLED и на старых строках.
- confirm_resolution VARCHAR(8) — исход confirm-паузы "yes"|"no" (петля калибровки словаря;
  раньше confirm_state="confirmed" не различал да/нет — инвентарь Фазы 0 §5.5).
Backwards-compatible: ADD COLUMN nullable, без backfill/блокировок; старый код колонки игнорирует.

Revision ID: 20260703_0075
Revises: 20260702_0074
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260703_0075"
down_revision = "20260702_0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("react_turn_trace", sa.Column("turn_policy_json", sa.Text(), nullable=True))
    op.add_column("react_turn_trace", sa.Column("confirm_resolution", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("react_turn_trace", "confirm_resolution")
    op.drop_column("react_turn_trace", "turn_policy_json")
