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
возможны частично созданные объекты; повторный accurate прогон безопасен
(``IF NOT EXISTS`` на конкуррентных индексах).

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


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # План #138: не висеть на локах под живым трафиком — лучше упасть и
    # повторить, чем застопорить прод. (Ограничивает ОЖИДАНИЕ лока; удержание
    # минимизировано ниже за счёт CONCURRENTLY / NOT VALID+VALIDATE.)
    op.execute("SET lock_timeout = '5s'")

    for child, parent, fk_col, ondelete in _PAIRS:
        uq_name = f"uq_{parent}_id_tenant"
        # 1. UNIQUE (id, tenant_id) на родителе (target composite-FK) без
        #    блокировки записи: конкуррентный уникальный индекс → повысить до
        #    constraint через USING INDEX (короткий ACCESS EXCLUSIVE, без скана).
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {uq_name} "
                f"ON {parent} (id, tenant_id)"
            )
        op.execute(
            f"ALTER TABLE {parent} ADD CONSTRAINT {uq_name} "
            f"UNIQUE USING INDEX {uq_name}"
        )

        # 2. Колонка nullable — бэкфилл — NOT NULL (сирот нет: FK на родителя и
        #    так NOT NULL; таблица мала → SET NOT NULL дёшев).
        op.add_column(
            child, sa.Column("tenant_id", sa.String(64), nullable=True)
        )
        op.execute(
            f"UPDATE {child} c SET tenant_id = p.tenant_id "
            f"FROM {parent} p WHERE c.{fk_col} = p.id"
        )
        op.alter_column(child, "tenant_id", nullable=False)

        # 3. Composite-FK ребёнок(fk_col, tenant_id) → родитель(id, tenant_id) и
        #    FK на tenants — добавляем NOT VALID (без скана под ACCESS EXCLUSIVE).
        ondelete_sql = f" ON DELETE {ondelete}" if ondelete else ""
        op.execute(
            f"ALTER TABLE {child} ADD CONSTRAINT fk_{child}_parent_tenant "
            f"FOREIGN KEY ({fk_col}, tenant_id) "
            f"REFERENCES {parent} (id, tenant_id){ondelete_sql} NOT VALID"
        )
        op.execute(
            f"ALTER TABLE {child} ADD CONSTRAINT fk_{child}_tenant "
            f"FOREIGN KEY (tenant_id) REFERENCES tenants (id) NOT VALID"
        )

        # 4. VALIDATE (лёгкий SHARE UPDATE EXCLUSIVE, запись идёт) + индекс под
        #    RLS-фильтр CONCURRENTLY — всё вне транзакции.
        with op.get_context().autocommit_block():
            op.execute(
                f"ALTER TABLE {child} VALIDATE CONSTRAINT fk_{child}_parent_tenant"
            )
            op.execute(
                f"ALTER TABLE {child} VALIDATE CONSTRAINT fk_{child}_tenant"
            )
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_{child}_tenant "
                f"ON {child} (tenant_id)"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("SET lock_timeout = '5s'")

    for child, parent, _fk_col, _ondelete in reversed(_PAIRS):
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS ix_{child}_tenant")
        op.drop_constraint(f"fk_{child}_tenant", child, type_="foreignkey")
        op.drop_constraint(
            f"fk_{child}_parent_tenant", child, type_="foreignkey"
        )
        # DROP CONSTRAINT снимает и уникальный индекс, созданный USING INDEX.
        op.drop_constraint(f"uq_{parent}_id_tenant", parent, type_="unique")
        op.drop_column(child, "tenant_id")
