"""#262 A3: assistant_memories.category_id → NOT NULL.

ПОСЛЕ backfill (0058) И выкатки кода, где save() резолвит Common (порядок деплоя план #262:
схема nullable → рестарт кода → backfill → эта NOT NULL). Если накатить до рестарта — старый процесс
писал бы NULL и упал. batch recreate — SQLite-совместимость (прод = Postgres).
"""

import sqlalchemy as sa
from alembic import op

revision = "20260629_0059"
down_revision = "20260629_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Префлайт (ревью R2): если backfill 0058 не прогнан ИЛИ код (save резолвит Common) не выкачен — остались
    # NULL, и SET NOT NULL упадёт ПОЗДНО непрозрачной ошибкой под ACCESS EXCLUSIVE-локом. Падаем рано и явно.
    remaining = bind.execute(
        sa.text("SELECT count(*) FROM assistant_memories WHERE category_id IS NULL")
    ).scalar()
    if remaining:
        raise RuntimeError(
            f"#262 0059: {remaining} строк assistant_memories с category_id IS NULL — сначала прогони backfill "
            "0058 и убедись, что код (save() резолвит Common) выкачен и перезапущен. SET NOT NULL прерван."
        )
    # M1 (ревью R1): на Postgres — прямой ALTER ... SET NOT NULL (скан без rewrite таблицы), НЕ recreate.
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("assistant_memories", schema=None, recreate="always") as batch:
            batch.alter_column("category_id", existing_type=sa.String(64), nullable=False)
    else:
        op.alter_column("assistant_memories", "category_id", existing_type=sa.String(64), nullable=False)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("assistant_memories", schema=None, recreate="always") as batch:
            batch.alter_column("category_id", existing_type=sa.String(64), nullable=True)
    else:
        op.alter_column("assistant_memories", "category_id", existing_type=sa.String(64), nullable=True)
