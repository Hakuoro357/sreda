"""#138 Ф3-a: денормализация tenant_id в 4 дочерние таблицы.

Дети (checklist_items, recipe_ingredients, menu_plan_items,
payment_order_items) получают СВОЙ tenant_id NOT NULL + composite-FK
(parent_id, tenant_id) → parent(id, tenant_id) — механическая гарантия
совпадения tenant_id ребёнка с родительским, иначе RLS-политику не
повесить.

Проверяем:
  * модельный слой: у всех 4 детей колонка tenant_id NOT NULL;
  * composite-FK присутствует (fk_col + tenant_id → parent.id +
    parent.tenant_id);
  * e2e через сервис: пункт чек-листа, созданный ChecklistService,
    наследует tenant_id родительского списка.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.services.checklists import ChecklistService

# Импорт моделей регистрирует таблицы в Base.metadata.
import sreda.db.models.billing  # noqa: F401
import sreda.db.models.checklists  # noqa: F401
import sreda.db.models.housewife_food  # noqa: F401


# (child_table, parent_table, fk_col)
PAIRS = (
    ("checklist_items", "checklists", "checklist_id"),
    ("recipe_ingredients", "recipes", "recipe_id"),
    ("menu_plan_items", "menu_plans", "menu_plan_id"),
    ("payment_order_items", "payment_orders", "payment_order_id"),
)


@pytest.mark.parametrize("child, parent, fk_col", PAIRS)
def test_child_has_tenant_id_not_null(child, parent, fk_col):
    table = Base.metadata.tables[child]
    assert "tenant_id" in table.columns, f"{child}: нет колонки tenant_id"
    col = table.columns["tenant_id"]
    assert col.nullable is False, f"{child}.tenant_id должен быть NOT NULL"


@pytest.mark.parametrize("child, parent, fk_col", PAIRS)
def test_child_has_composite_fk_to_parent(child, parent, fk_col):
    table = Base.metadata.tables[child]
    expected_cols = {fk_col, "tenant_id"}
    composite = None
    for fkc in table.foreign_key_constraints:
        if {c.name for c in fkc.columns} == expected_cols:
            composite = fkc
            break
    assert composite is not None, (
        f"{child}: нет composite-FK ({fk_col}, tenant_id)"
    )
    referred = {fk.column.table.name for fk in composite.elements}
    assert referred == {parent}, (
        f"{child}: composite-FK должен указывать на {parent}, "
        f"а указывает на {referred}"
    )
    referred_cols = {fk.column.name for fk in composite.elements}
    assert referred_cols == {"id", "tenant_id"}


@pytest.mark.parametrize("parent", sorted({p for _, p, _ in PAIRS}))
def test_parent_has_id_tenant_unique(parent):
    """Composite-FK требует UNIQUE (id, tenant_id) на родителе."""
    from sqlalchemy import UniqueConstraint

    table = Base.metadata.tables[parent]
    uniques = [
        c for c in table.constraints
        if isinstance(c, UniqueConstraint)
        and {col.name for col in c.columns} == {"id", "tenant_id"}
    ]
    assert uniques, f"{parent}: нет UNIQUE (id, tenant_id)"


def test_checklist_item_inherits_tenant_id_e2e():
    """e2e через сервис: пункт наследует tenant_id родительского списка."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="t1", name="Test"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    s.commit()

    svc = ChecklistService(s)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="Дела на дачу")
    items, _ = svc.add_items(list_id=cl.id, items=["Полить огурцы"])
    assert len(items) == 1
    assert items[0].tenant_id == "t1"

    # И через no-commit вариант (второй writer-сайт).
    items2, _ = svc.add_items_no_commit(list_id=cl.id, items=["Прополоть"])
    s.commit()
    assert len(items2) == 1
    assert items2[0].tenant_id == "t1"
