"""#202 семья 1 (recipes): оснащение save_recipe ключами идемпотентности (эталон reminders.schedule).

RED-before-impl. Контракт:
- ctx-путь (ReAct) → recipe.operation_id + normalized_title_hash ЗАПОЛНЕНЫ (key-policy для гейта Фазы 5);
  повтор того же контента в ходе → 1 строка (named-X/Y).
- легаси (ctx None) → ключи НЕ заполняются, поведение байт-в-байт (fuzzy-дедуп).
- _FAMILY_WRITE_POLICY["recipes"] == "idempotent" (после оснащения семья прунабельна).
Плайн-сессия (не conftest) — begin_nested/commit не маскируются StaticPool-обёрткой.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife_food import Recipe
from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from sreda.services.housewife_recipes import HousewifeRecipeService


@pytest.fixture()
def s():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(User(id="u1", tenant_id="t1"))
    sess.commit()
    yield sess
    sess.close()


def _ctx(step="s1"):
    return ToolRuntimeContext(
        operation_id="o1", execution_id="e1", step_id=step,
        tool_name="save_recipe", tenant_id="t1", user_id="u1", turn_key="tk1")


def test_save_recipe_ctx_populates_keys_202(s):
    """ctx-путь → operation_id + normalized_title_hash заполнены (key-policy)."""
    svc = HousewifeRecipeService(s)
    with bind_tool_runtime(_ctx()):
        r, is_new = svc.save_recipe(tenant_id="t1", user_id="u1", title="Борщ", ingredients=[])
    assert is_new is True
    s.refresh(r)
    assert r.operation_id is not None, "ctx-путь должен записать operation_id (key-policy)"
    assert r.normalized_title_hash is not None, "ctx-путь должен записать normalized_title_hash"


def test_save_recipe_ctx_replay_single_row_202(s):
    """Повтор save_recipe того же контента в одном ходе (тот же ctx) → ровно 1 рецепт + тот же id (named-X/Y)."""
    svc = HousewifeRecipeService(s)
    with bind_tool_runtime(_ctx()):
        r1, new1 = svc.save_recipe(tenant_id="t1", user_id="u1", title="Борщ", ingredients=[])
        r2, new2 = svc.save_recipe(tenant_id="t1", user_id="u1", title="Борщ", ingredients=[])
    assert new1 is True and new2 is False
    assert r1.id == r2.id
    assert s.query(Recipe).count() == 1


def test_save_recipe_legacy_no_ctx_keys_none_202(s):
    """Легаси (ctx None) → ключи НЕ заполняются, поведение прежнее (регресс-гард)."""
    svc = HousewifeRecipeService(s)
    r, is_new = svc.save_recipe(tenant_id="t1", user_id="u1", title="Плов", ingredients=[])
    assert is_new is True
    s.refresh(r)
    assert r.operation_id is None, "легаси-путь не должен трогать operation_id"


def test_recipes_policy_idempotent_202():
    """После оснащения семья recipes прунабельна → политика 'idempotent' (не 'unkeyed')."""
    from sreda.runtime.react_loop import _FAMILY_WRITE_POLICY
    assert _FAMILY_WRITE_POLICY["recipes"] == "idempotent"


def test_save_recipes_batch_ctx_populates_keys_202(s):
    """Батч на ctx-пути → каждый рецепт получает свой operation_id (по своему title) + hash."""
    svc = HousewifeRecipeService(s)
    with bind_tool_runtime(_ctx()):
        res = svc.save_recipes_batch(
            tenant_id="t1", user_id="u1",
            recipes=[{"title": "Борщ", "ingredients": []},
                     {"title": "Плов", "ingredients": []}])
    rows = s.query(Recipe).order_by(Recipe.title).all()
    assert len(rows) == 2
    op_ids = {r.operation_id for r in rows}
    assert None not in op_ids, "каждый рецепт батча должен иметь operation_id"
    assert len(op_ids) == 2, "разные рецепты (разный title) → разные operation_id"
    assert all(r.normalized_title_hash is not None for r in rows)
