"""#138 Ф3-a: денормализация tenant_id в 4 дочерние таблицы.

Дети (checklist_items, recipe_ingredients, menu_plan_items,
payment_order_items) не имели tenant_id — только FK на родителя, RLS-политику
не повесить. Даём каждому СВОЙ tenant_id NOT NULL + composite-FK
(parent_id, tenant_id) → parent(id, tenant_id): механическая гарантия, что
tenant_id ребёнка совпадает с родительским (нельзя подделать).

Пары (ребёнок → родитель):
  * checklist_items      → checklists      (checklist_id, CASCADE)
  * recipe_ingredients   → recipes         (recipe_id, CASCADE)
  * menu_plan_items      → menu_plans      (menu_plan_id, CASCADE)
  * payment_order_items  → payment_orders  (payment_order_id, без ondelete —
    как у существующей одноколоночной FK)

Лок-профиль (M10, прод-накат под трафиком). `lock_timeout` ограничивает лишь
ОЖИДАНИЕ лока, НЕ время УДЕРЖАНИЯ. Поэтому тяжёлые операции разведены так, чтобы
не держать ACCESS EXCLUSIVE со сканом на живой таблице:
  * UNIQUE (id, tenant_id) на родителе — строим ``CREATE UNIQUE INDEX
    CONCURRENTLY`` (не блокирует запись), затем ``ADD CONSTRAINT ... USING
    INDEX`` (короткий ACCESS EXCLUSIVE, без скана).
  * composite-FK и FK на tenants добавляем ``NOT VALID`` (без скана под
    ACCESS EXCLUSIVE), затем отдельным шагом ``VALIDATE CONSTRAINT`` (лёгкий
    SHARE UPDATE EXCLUSIVE, запись не блокируется).
  * индекс ребёнка под RLS-фильтр — ``CREATE INDEX CONCURRENTLY``.
  * SET NOT NULL на ребёнке оставлен как есть: колонка только что создана и
    забэкфилена в этой же миграции, дочерние таблицы малы (альфа ~200-300
    юзеров), скан дешёвый; выносить в CHECK…NOT VALID→VALIDATE было бы
    оверинжинирингом.
CONCURRENTLY нельзя в транзакции → эти шаги в ``op.get_context().autocommit_block()``
(env.py гонит весь прогон одной транзакцией; autocommit_block временно её
коммитит). Как следствие миграция НЕ атомарна — при падении между шагами
(например по lock_timeout: стратегия «упасть и повторить») остаются частично
созданные объекты.

Идемпотентность повторного прогона (R2 consensus-MAJOR): каждый шаг закрыт
catalog-гардом, поэтому повторный ``alembic upgrade`` безопасен после падения
на ЛЮБОМ шаге:
  * колонки — ``ADD COLUMN IF NOT EXISTS``;
  * бэкфилл — ``WHERE c.tenant_id IS NULL`` (не перезатирает);
  * SET NOT NULL — идемпотентен нативно;
  * CONCURRENTLY-индексы — перед созданием проверяем ``pg_index.indisvalid``:
    упавший ``CREATE INDEX CONCURRENTLY`` оставляет INVALID-индекс, который
    ``IF NOT EXISTS`` молча пропустил бы → INVALID сносим
    (``DROP INDEX CONCURRENTLY``) и строим заново; валидный — скип;
  * констрейнты (UNIQUE USING INDEX, composite-FK, FK) — скип, если conname
    уже есть в ``pg_constraint`` для этой таблицы;
  * VALIDATE — только если констрейнт ещё ``NOT convalidated``.
downgrade симметрично устойчив (``DROP ... IF EXISTS`` на каждом шаге).

PG-guard: unit-тесты строят схему через Base.metadata.create_all (SQLite),
прод = PG — на не-PG миграция no-op (прецедент 0078).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260709_0080"
down_revision = "20260709_0079"
branch_labels = None
depends_on = None


# (child, parent, fk_col, ondelete)
_PAIRS = (
    ("checklist_items", "checklists", "checklist_id", "CASCADE"),
    ("recipe_ingredients", "recipes", "recipe_id", "CASCADE"),
    ("menu_plan_items", "menu_plans", "menu_plan_id", "CASCADE"),
    ("payment_order_items", "payment_orders", "payment_order_id", None),
)


# ── catalog-гарды перезапускаемости (см. докстринг) ─────────────────────────
# Продублированы в 0081 — миграции самодостаточны, общий helper-модуль не
# заводим сознательно.


def _index_state(bind, index_name: str) -> str | None:
    """None — индекса нет; "valid"/"invalid" — по pg_index.indisvalid.

    R3 MINOR (medium): фильтр по ``relname`` без ``pg_namespace`` — допущение
    single-schema (миграции Среды гоняются на ``public``, search_path не кастомим).
    При кастомном search_path одноимённый индекс в другой схеме дал бы ложное совпадение;
    здесь это не наш кейс. Явно фиксирую допущение, чтобы не расширять запрос без нужды."""
    row = bind.execute(
        sa.text(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name"
        ),
        {"name": index_name},
    ).first()
    if row is None:
        return None
    return "valid" if row[0] else "invalid"


def _constraint_exists(bind, table: str, conname: str) -> bool:
    return (
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = :c AND conrelid = CAST(:t AS regclass)"
            ),
            {"c": conname, "t": table},
        ).first()
        is not None
    )


def _constraint_needs_validate(bind, table: str, conname: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = :c AND conrelid = CAST(:t AS regclass)"
        ),
        {"c": conname, "t": table},
    ).first()
    return row is not None and not row[0]


def _create_index_concurrently(bind, index_name: str, ddl: str) -> None:
    """CONCURRENTLY-индекс с restart-гардом: упавший CREATE INDEX CONCURRENTLY
    оставляет INVALID-индекс (IF NOT EXISTS его молча пропустил бы, а
    USING INDEX упал бы) → INVALID сносим и строим заново; валидный — скип."""
    state = _index_state(bind, index_name)
    if state == "valid":
        return
    with op.get_context().autocommit_block():
        if state == "invalid":
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
        op.execute(ddl)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # План #138: не висеть на локах под живым трафиком — лучше упасть и
    # повторить, чем застопорить прод. (Ограничивает ОЖИДАНИЕ лока; удержание
    # минимизировано ниже за счёт CONCURRENTLY / NOT VALID+VALIDATE. Повторный
    # прогон после падения безопасен — catalog-гарды на каждом шаге.)
    op.execute("SET lock_timeout = '5s'")

    for child, parent, fk_col, ondelete in _PAIRS:
        uq_name = f"uq_{parent}_id_tenant"
        # 1. UNIQUE (id, tenant_id) на родителе (target composite-FK) без
        #    блокировки записи: конкуррентный уникальный индекс → повысить до
        #    constraint через USING INDEX (короткий ACCESS EXCLUSIVE, без скана).
        #    Гард: constraint уже есть → оба шага скип (его индекс валиден).
        if not _constraint_exists(bind, parent, uq_name):
            _create_index_concurrently(
                bind,
                uq_name,
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {uq_name} "
                f"ON {parent} (id, tenant_id)",
            )
            op.execute(
                f"ALTER TABLE {parent} ADD CONSTRAINT {uq_name} "
                f"UNIQUE USING INDEX {uq_name}"
            )

        # 2. Колонка nullable — бэкфилл — NOT NULL (сирот нет: FK на родителя и
        #    так NOT NULL; таблица мала → SET NOT NULL дёшев). IF NOT EXISTS +
        #    WHERE tenant_id IS NULL: повторный прогон не падает и не
        #    перезатирает; SET NOT NULL идемпотентен нативно.
        op.execute(
            f"ALTER TABLE {child} ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64)"
        )
        op.execute(
            f"UPDATE {child} c SET tenant_id = p.tenant_id "
            f"FROM {parent} p WHERE c.{fk_col} = p.id AND c.tenant_id IS NULL"
        )
        op.alter_column(child, "tenant_id", nullable=False)

        # 3. Composite-FK ребёнок(fk_col, tenant_id) → родитель(id, tenant_id) и
        #    FK на tenants — добавляем NOT VALID (без скана под ACCESS EXCLUSIVE).
        #    Гард: скип, если constraint уже есть.
        ondelete_sql = f" ON DELETE {ondelete}" if ondelete else ""
        if not _constraint_exists(bind, child, f"fk_{child}_parent_tenant"):
            op.execute(
                f"ALTER TABLE {child} ADD CONSTRAINT fk_{child}_parent_tenant "
                f"FOREIGN KEY ({fk_col}, tenant_id) "
                f"REFERENCES {parent} (id, tenant_id){ondelete_sql} NOT VALID"
            )
        if not _constraint_exists(bind, child, f"fk_{child}_tenant"):
            op.execute(
                f"ALTER TABLE {child} ADD CONSTRAINT fk_{child}_tenant "
                f"FOREIGN KEY (tenant_id) REFERENCES tenants (id) NOT VALID"
            )

        # 4. VALIDATE (лёгкий SHARE UPDATE EXCLUSIVE, запись идёт; только если
        #    ещё NOT convalidated) + индекс под RLS-фильтр CONCURRENTLY — всё
        #    вне транзакции.
        with op.get_context().autocommit_block():
            for conname in (f"fk_{child}_parent_tenant", f"fk_{child}_tenant"):
                if _constraint_needs_validate(bind, child, conname):
                    op.execute(
                        f"ALTER TABLE {child} VALIDATE CONSTRAINT {conname}"
                    )
        _create_index_concurrently(
            bind,
            f"ix_{child}_tenant",
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_{child}_tenant "
            f"ON {child} (tenant_id)",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("SET lock_timeout = '5s'")

    for child, parent, _fk_col, _ondelete in reversed(_PAIRS):
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS ix_{child}_tenant")
        op.execute(
            f"ALTER TABLE {child} DROP CONSTRAINT IF EXISTS fk_{child}_tenant"
        )
        op.execute(
            f"ALTER TABLE {child} DROP CONSTRAINT IF EXISTS "
            f"fk_{child}_parent_tenant"
        )
        # DROP CONSTRAINT снимает и уникальный индекс, созданный USING INDEX;
        # если после частичного upgrade остался ТОЛЬКО индекс (constraint не
        # успел создаться) — добираем его отдельным DROP INDEX IF EXISTS.
        op.execute(
            f"ALTER TABLE {parent} DROP CONSTRAINT IF EXISTS uq_{parent}_id_tenant"
        )
        op.execute(f"DROP INDEX IF EXISTS uq_{parent}_id_tenant")
        op.execute(f"ALTER TABLE {child} DROP COLUMN IF EXISTS tenant_id")
