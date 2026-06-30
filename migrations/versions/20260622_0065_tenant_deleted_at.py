"""#187 soft-delete — tenants.deleted_at + partial-индекс (рубильник отключения тенанта).

Один флаг на тенанте: NULL = активен; не-null = помечен удалённым (обратимо, restore снимает).
В таблицы данных признак НЕ добавляем. Колонка nullable, **БЕЗ backfill** — существующие тенанты
остаются активными (deleted_at=NULL), в отличие от approved_at (0023), который проставлял NOW().

Partial-индекс ``WHERE deleted_at IS NOT NULL`` — только для админ-листинга удалённых тенантов;
гейт is_tenant_active идёт по PK. На момент миграции индекс ПУСТ (все NULL) → запись мгновенна, но
plain CREATE INDEX берёт SHARE-лок + сканирует таблицу для предиката; на PG ``SET lock_timeout``
делает build fail-fast (как 0062). На альфа-масштабе tenants крошечная → суб-секунда.

Revision ID: 20260622_0065
Revises: 20260622_0064
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260622_0065"
down_revision = "20260622_0064"
branch_labels = None
depends_on = None

_IDX = "ix_tenants_deleted_at"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
    op.add_column(
        "tenants",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        _IDX,
        "tenants",
        ["deleted_at"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
        sqlite_where=sa.text("deleted_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_IDX, table_name="tenants")
    op.drop_column("tenants", "deleted_at")
