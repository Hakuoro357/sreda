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
    # повторить, чем застопорить прод.
    op.execute("SET lock_timeout = '5s'")

    for child, parent, fk_col, ondelete in _PAIRS:
        # 1. Колонка nullable — бэкфилл — NOT NULL (сирот нет: FK на
        #    родителя и так NOT NULL).
        op.add_column(
            child, sa.Column("tenant_id", sa.String(64), nullable=True)
        )
        op.execute(
            f"UPDATE {child} c SET tenant_id = p.tenant_id "
            f"FROM {parent} p WHERE c.{fk_col} = p.id"
        )
        op.alter_column(child, "tenant_id", nullable=False)

        # 2. UNIQUE (id, tenant_id) на родителе — формальный (id и так PK),
        #    нужен как target для composite-FK.
        op.create_unique_constraint(
            f"uq_{parent}_id_tenant", parent, ["id", "tenant_id"]
        )

        # 3. Composite-FK ребёнок(fk_col, tenant_id) → родитель(id, tenant_id).
        fk_kwargs = {"ondelete": ondelete} if ondelete else {}
        op.create_foreign_key(
            f"fk_{child}_parent_tenant",
            child,
            parent,
            [fk_col, "tenant_id"],
            ["id", "tenant_id"],
            **fk_kwargs,
        )

        # 4. FK на tenants + индекс под RLS-фильтр.
        op.create_foreign_key(
            f"fk_{child}_tenant", child, "tenants", ["tenant_id"], ["id"]
        )
        op.create_index(f"ix_{child}_tenant", child, ["tenant_id"])


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("SET lock_timeout = '5s'")

    for child, parent, _fk_col, _ondelete in reversed(_PAIRS):
        op.drop_index(f"ix_{child}_tenant", table_name=child)
        op.drop_constraint(f"fk_{child}_tenant", child, type_="foreignkey")
        op.drop_constraint(
            f"fk_{child}_parent_tenant", child, type_="foreignkey"
        )
        op.drop_constraint(
            f"uq_{parent}_id_tenant", parent, type_="unique"
        )
        op.drop_column(child, "tenant_id")
