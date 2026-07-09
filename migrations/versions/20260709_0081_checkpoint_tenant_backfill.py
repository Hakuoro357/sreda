"""#138 Ф3-b: денормализация tenant_id в react_checkpoint / react_checkpoint_write + бэкфилл.

Обе таблицы — весь шифрованный диалог ReAct, БЕЗ tenant_id; PK thread_id = `react-v1:{hmac}` —
тенант из ключа НЕ восстановим. Мост для бэкфилла: react_turn_trace хранит tenant_id + СЫРОЙ
base в колонке thread_id → durable-ключ восстанавливаем той же деривацией, что react_loop
(`_durable_thread_id`): hmac-sha256(SREDA_ENCRYPTION_KEY, base) с префиксом "react-v1:".

Колонка NULLABLE: orphan-строки (тред без следа в react_turn_trace) остаются NULL — под RLS
Ф3-c их видит только maintenance (fail-closed видимость); карантин/удаление = owner-решение Ф5.

Самодостаточность: секрет берём из os.environ (НЕ импортируем app-код — миграции
самодостаточны). Collision-abort: один base у двух разных tenant_id → RuntimeError (не молча).

Лок-профиль (M10, прод-накат под трафиком). react_checkpoint /
react_checkpoint_write — крупные (весь шифрованный диалог ReAct). Поэтому:
  * ADD COLUMN nullable без DEFAULT = catalog-only, мгновенно (короткий ACCESS
    EXCLUSIVE под lock_timeout).
  * бэкфилл — точечные UPDATE по durable-ключу (thread_id — часть PK, индексирован),
    берут ROW EXCLUSIVE, чтение/чужие строки не блокируются.
  * индексы под RLS-фильтр — ``CREATE INDEX CONCURRENTLY`` (обычный build на
    крупной таблице держал бы ACCESS EXCLUSIVE весь скан). Строим ПОСЛЕ бэкфилла,
    чтобы не поддерживать индекс во время bulk-UPDATE.
CONCURRENTLY нельзя в транзакции → индексы в ``op.get_context().autocommit_block()``
(env.py гонит прогон одной транзакцией; блок временно её коммитит → миграция не
атомарна, но ``IF NOT EXISTS`` делает повторный прогон безопасным).

PG-guard: unit-тесты строят схему через Base.metadata.create_all (SQLite), прод = PG —
на не-PG миграция no-op (прецедент 0078/0080).
"""

from __future__ import annotations

import hashlib
import hmac
import os

import sqlalchemy as sa
from alembic import op

revision = "20260709_0081"
down_revision = "20260709_0080"
branch_labels = None
depends_on = None

_TABLES = ("react_checkpoint", "react_checkpoint_write")
_INDEXES = {
    "react_checkpoint": "ix_react_checkpoint_tenant",
    "react_checkpoint_write": "ix_react_checkpoint_write_tenant",
}


def _durable(secret: str, base: str) -> str:
    """Деривация durable thread_id — ТОЧНАЯ копия react_loop._durable_thread_id:
    `react-v1:{hmac_sha256(secret, base)}` (сверяется тестом test_138_checkpoint_tenant)."""
    digest = hmac.new(
        secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"react-v1:{digest}"


def _collect_tenant_by_base(rows) -> dict[str, str]:
    """(tenant_id, base)-пары → {base: tenant}. Collision-abort: один base у ДВУХ разных
    tenant_id → RuntimeError с перечнем (бэкфилл по такому мосту опасен — не молча)."""
    tenants_by_base: dict[str, set[str]] = {}
    for tenant_id, base in rows:
        if tenant_id is None or base is None:
            continue
        tenants_by_base.setdefault(base, set()).add(tenant_id)
    collisions = {b: sorted(ts) for b, ts in tenants_by_base.items() if len(ts) > 1}
    if collisions:
        detail = "; ".join(f"{b} -> {ts}" for b, ts in sorted(collisions.items()))
        raise RuntimeError(
            "0081 бэкфилл: collision — один thread base у нескольких tenant_id, "
            f"бэкфилл прерван: {detail}"
        )
    return {b: next(iter(ts)) for b, ts in tenants_by_base.items()}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    secret = os.environ.get("SREDA_ENCRYPTION_KEY")
    if not secret:
        raise RuntimeError("0081: бэкфилл требует SREDA_ENCRYPTION_KEY в окружении")

    # Не висеть на локах под трафиком (ограничивает ОЖИДАНИЕ; удержание
    # минимизировано CONCURRENTLY-индексами ниже).
    op.execute("SET lock_timeout = '5s'")

    # 1. Колонки (nullable — легаси/orphan остаются NULL). ADD COLUMN без DEFAULT
    #    = catalog-only, мгновенно. Индексы — ниже, CONCURRENTLY, после бэкфилла.
    for table in _TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.String(64), nullable=True))

    # 2. Data-бэкфилл через мост react_turn_trace (tenant_id + сырой base).
    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT tenant_id, thread_id FROM react_turn_trace "
            "WHERE thread_id IS NOT NULL"
        )
    ).all()
    tenant_by_base = _collect_tenant_by_base(rows)

    upd_cp = sa.text(
        "UPDATE react_checkpoint SET tenant_id = :t "
        "WHERE thread_id = :d AND tenant_id IS NULL"
    )
    upd_w = sa.text(
        "UPDATE react_checkpoint_write SET tenant_id = :t "
        "WHERE thread_id = :d AND tenant_id IS NULL"
    )
    for base, tenant in tenant_by_base.items():
        durable = _durable(secret, base)
        bind.execute(upd_cp, {"t": tenant, "d": durable})
        bind.execute(upd_w, {"t": tenant, "d": durable})

    # 3. Индексы под RLS-фильтр — CONCURRENTLY (крупные таблицы; обычный build
    #    держал бы ACCESS EXCLUSIVE весь скан). Вне транзакции.
    with op.get_context().autocommit_block():
        for table in _TABLES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEXES[table]} "
                f"ON {table} (tenant_id)"
            )

    # 4. Orphan-preflight: NULL-строки остаются (maintenance-only под RLS), НЕ удаляем.
    n_cp = bind.execute(
        sa.text("SELECT count(*) FROM react_checkpoint WHERE tenant_id IS NULL")
    ).scalar()
    n_w = bind.execute(
        sa.text("SELECT count(*) FROM react_checkpoint_write WHERE tenant_id IS NULL")
    ).scalar()
    print(
        f"0081 orphans: checkpoint={n_cp} write={n_w} — остаются NULL "
        "(maintenance-only), карантин/удаление = owner-решение Ф5"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("SET lock_timeout = '5s'")
    for table in reversed(_TABLES):
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEXES[table]}")
        op.drop_column(table, "tenant_id")
