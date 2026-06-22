"""#163 Фаза 2b — partial-UNIQUE индекс межходового замка от дублей (ReAct).

Инвариант: не более ОДНОГО *pending* напоминания/задачи с одним semantic_key
``(tenant_id, COALESCE(user_id,'__tenant_wide__'), normalized_title_hash)`` на ``family_reminders``
и ``tasks_items``. COALESCE-заглушка — чтобы ОБЩЕСЕМЕЙНЫЕ (NULL user) тоже дедупились (иначе NULL≠NULL
в UNIQUE их бы не схлопнул). Партиал ``WHERE status='pending' AND normalized_title_hash IS NOT NULL``:
не-pending и легаси NULL-hash — ВНЕ замка (scope #163: semantic_key пишется только на ReAct/ctx пути).
Имена индексов и предикат ТОЧНО как в моделях (housewife.py / tasks.py) — миграция и create_all
строят один и тот же индекс.

Паттерн (как 0048, ревью Codex R1): LOCK TABLE IN EXCLUSIVE MODE + lock_timeout → дедуп-аудит →
ПЛАИН (блокирующий) CREATE UNIQUE INDEX, НЕ CONCURRENTLY. Таблицы крошечные (прод 2026-06-22:
~27 напоминаний + ~48 задач) → build суб-секунда, лок держится миг, всё атомарно в транзакции
alembic (CONCURRENTLY не может в транзакции и оставил бы INVALID-индекс при сбое — 0048 это
обосновал). EXCLUSIVE-лок закрывает окно «писатель создаёт дубль между аудитом и CREATE INDEX».

Дедуп-аудит: если уже есть >1 pending-строки с одним ключом → ABORT. НЕ авто-удаляем строки —
это пользовательские напоминания/задачи; дубли разрешает владелец вручную. Прод-инвентаризация
2026-06-22: 0 дублей → аудит пройдёт. (Reuse-on-conflict в коде 2b не даёт НОВЫХ дублей на ReAct.)

Перед прогоном на проде — БЭКАП БД (обязателен). Downgrade — drop индекса (данные не трогает).

Примечание: alembic autogenerate НЕ умеет рефлексировать выражение-индекс (COALESCE) —
может «не видеть» его и предлагать пере-создать; это известное ограничение, не баг миграции.

Revision ID: 20260622_0060
Revises: 20260622_0059
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260622_0060"
down_revision = "20260622_0059"
branch_labels = None
depends_on = None

# (таблица, имя индекса) — имена ТОЧНО как в моделях.
_TABLES = (
    ("family_reminders", "uq_family_reminders_pending_semantic"),
    ("tasks_items", "uq_tasks_items_pending_semantic"),
)
_PREDICATE = "status = 'pending' AND normalized_title_hash IS NOT NULL"


def _create_index_sql(table: str, idx: str) -> str:
    # Портируемо PG+SQLite: оба поддерживают выражение-индекс (COALESCE), партиал WHERE, IF NOT EXISTS.
    return (
        f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON {table} "
        f"(tenant_id, COALESCE(user_id, '__tenant_wide__'), normalized_title_hash) "
        f"WHERE {_PREDICATE}"
    )


def _audit_no_dups(bind, table: str) -> None:
    """ABORT, если уже есть >1 pending-строки с одним semantic_key (иначе CREATE UNIQUE упадёт).
    НЕ авто-удаляем (пользовательские данные) — требуем ручного разбора (план #163)."""
    n_groups = bind.execute(sa.text(
        f"SELECT COUNT(*) FROM ("  # noqa: S608 — table из доверенного литерала _TABLES, не ввод
        f"  SELECT 1 FROM {table} WHERE {_PREDICATE}"
        f"  GROUP BY tenant_id, COALESCE(user_id, '__tenant_wide__'), normalized_title_hash"
        f"  HAVING COUNT(*) > 1"
        f") d"
    )).scalar()
    if n_groups:
        raise RuntimeError(
            f"#163 2b: в {table} найдено {n_groups} групп дублей среди pending — уникальный индекс "
            f"не создать. Разреши дубли ВРУЧНУЮ (НЕ авто-удаляем пользовательские данные) и повтори."
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Закрываем окно гонки аудит→CREATE INDEX (как 0048). Таблицы крошечные → лок суб-секунда.
        bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        for table, _idx in _TABLES:
            bind.execute(sa.text(f"LOCK TABLE {table} IN EXCLUSIVE MODE"))
    for table, idx in _TABLES:
        _audit_no_dups(bind, table)
        op.execute(_create_index_sql(table, idx))


def downgrade() -> None:
    for _table, idx in _TABLES:
        op.execute(f"DROP INDEX IF EXISTS {idx}")
