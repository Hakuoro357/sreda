"""Unit tests for HousewifeMenuService — weekly menu planning."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife_food import MenuPlan, MenuPlanItem
from sreda.services.housewife_menu import (
    HousewifeMenuService,
    MenuCellInput,
    _coerce_monday,
)
from sreda.services.housewife_recipes import HousewifeRecipeService


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.commit()
    yield sess
    sess.close()


# ---------------------------------------------------------------------------
# _coerce_monday
# ---------------------------------------------------------------------------


def test_coerce_monday_from_date_middle_of_week():
    # 2026-04-22 is a Wednesday — ISO week starts Monday 2026-04-20.
    assert _coerce_monday(date(2026, 4, 22)) == date(2026, 4, 20)


def test_coerce_monday_noop_for_monday():
    assert _coerce_monday(date(2026, 4, 20)) == date(2026, 4, 20)


def test_coerce_monday_from_string():
    assert _coerce_monday("2026-04-26") == date(2026, 4, 20)


# ---------------------------------------------------------------------------
# plan_week — basic create
# ---------------------------------------------------------------------------


def test_plan_week_creates_plan_and_items(session):
    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1", user_id="u1",
        week_start="2026-04-20",
        cells=[
            {"day_of_week": 0, "meal_type": "breakfast", "free_text": "овсянка"},
            {"day_of_week": 0, "meal_type": "lunch", "free_text": "борщ"},
            {"day_of_week": 1, "meal_type": "dinner", "free_text": "паста"},
        ],
    )
    assert plan.week_start_date == date(2026, 4, 20)
    items = session.query(MenuPlanItem).all()
    assert len(items) == 3


def test_plan_week_coerces_mid_week_date_to_monday(session):
    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1", user_id="u1",
        week_start="2026-04-23",  # Thursday
        cells=[{"day_of_week": 0, "meal_type": "breakfast", "free_text": "x"}],
    )
    assert plan.week_start_date == date(2026, 4, 20)


def test_plan_week_skips_empty_cells(session):
    svc = HousewifeMenuService(session)
    svc.plan_week(
        tenant_id="t1", user_id="u1",
        week_start="2026-04-20",
        cells=[
            {"day_of_week": 0, "meal_type": "breakfast", "free_text": "x"},
            {"day_of_week": 0, "meal_type": "lunch"},  # no recipe_id, no free_text
            {"day_of_week": 1, "meal_type": "breakfast", "free_text": "  "},
        ],
    )
    assert session.query(MenuPlanItem).count() == 1


def test_plan_week_drops_unknown_meal_type(session):
    svc = HousewifeMenuService(session)
    svc.plan_week(
        tenant_id="t1", user_id="u1",
        week_start="2026-04-20",
        cells=[
            {"day_of_week": 0, "meal_type": "breakfast", "free_text": "x"},
            {"day_of_week": 0, "meal_type": "brunch", "free_text": "y"},
        ],
    )
    assert session.query(MenuPlanItem).count() == 1


def test_plan_week_drops_out_of_range_day(session):
    svc = HousewifeMenuService(session)
    svc.plan_week(
        tenant_id="t1", user_id="u1",
        week_start="2026-04-20",
        cells=[
            {"day_of_week": 0, "meal_type": "breakfast", "free_text": "x"},
            {"day_of_week": 7, "meal_type": "breakfast", "free_text": "y"},
        ],
    )
    assert session.query(MenuPlanItem).count() == 1


def test_plan_week_replaces_prior_plan_for_same_week(session):
    svc = HousewifeMenuService(session)
    # First plan — 2 cells
    svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[
            {"day_of_week": 0, "meal_type": "breakfast", "free_text": "овсянка"},
            {"day_of_week": 0, "meal_type": "lunch", "free_text": "борщ"},
        ],
    )
    assert session.query(MenuPlan).count() == 1
    assert session.query(MenuPlanItem).count() == 2

    # Second plan for the SAME week — 1 cell, replaces the first
    svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[
            {"day_of_week": 1, "meal_type": "dinner", "free_text": "рыба"},
        ],
    )
    assert session.query(MenuPlan).count() == 1
    assert session.query(MenuPlanItem).count() == 1
    remaining = session.query(MenuPlanItem).first()
    assert remaining.free_text == "рыба"


def test_plan_week_accepts_recipe_id_cells(session):
    recipe_svc = HousewifeRecipeService(session)
    r = recipe_svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[{"title": "свёкла"}],
        source="user_dictated",
    )

    svc = HousewifeMenuService(session)
    svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[
            {"day_of_week": 0, "meal_type": "lunch", "recipe_id": r.id},
        ],
    )
    item = session.query(MenuPlanItem).first()
    assert item.recipe_id == r.id
    assert item.free_text is None


# ---------------------------------------------------------------------------
# update_item
# ---------------------------------------------------------------------------


def test_update_item_creates_if_missing(session):
    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[{"day_of_week": 0, "meal_type": "breakfast", "free_text": "x"}],
    )
    item = svc.update_item(
        tenant_id="t1", user_id="u1", plan_id=plan.id,
        day_of_week=3, meal_type="dinner", free_text="суши",
    )
    assert item is not None
    assert item.free_text == "суши"
    assert session.query(MenuPlanItem).count() == 2


def test_update_item_overwrites_existing(session):
    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[{"day_of_week": 0, "meal_type": "breakfast", "free_text": "овсянка"}],
    )
    item = svc.update_item(
        tenant_id="t1", user_id="u1", plan_id=plan.id,
        day_of_week=0, meal_type="breakfast", free_text="творог с ягодами",
    )
    assert item.free_text == "творог с ягодами"
    assert session.query(MenuPlanItem).count() == 1


def test_update_item_clears_cell_when_both_none(session):
    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[{"day_of_week": 0, "meal_type": "breakfast", "free_text": "x"}],
    )
    result = svc.update_item(
        tenant_id="t1", user_id="u1", plan_id=plan.id,
        day_of_week=0, meal_type="breakfast",
        recipe_id=None, free_text=None,
    )
    assert result is None
    assert session.query(MenuPlanItem).count() == 0


def test_update_item_cross_tenant_returns_none(session):
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()

    svc = HousewifeMenuService(session)
    my_plan = svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[{"day_of_week": 0, "meal_type": "breakfast", "free_text": "x"}],
    )
    result = svc.update_item(
        tenant_id="t2", user_id="u2", plan_id=my_plan.id,
        day_of_week=0, meal_type="lunch", free_text="impostor",
    )
    assert result is None
    # Original plan untouched
    assert session.query(MenuPlanItem).count() == 1


def test_update_item_rejects_unknown_meal_type(session):
    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[{"day_of_week": 0, "meal_type": "breakfast", "free_text": "x"}],
    )
    with pytest.raises(ValueError, match="meal_type"):
        svc.update_item(
            tenant_id="t1", user_id="u1", plan_id=plan.id,
            day_of_week=0, meal_type="brunch", free_text="y",
        )


# ---------------------------------------------------------------------------
# clear_menu / get_plan_for_week / list_user_plans
# ---------------------------------------------------------------------------


def test_clear_menu_removes_plans_for_week(session):
    svc = HousewifeMenuService(session)
    svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[{"day_of_week": 0, "meal_type": "breakfast", "free_text": "x"}],
    )
    n = svc.clear_menu(tenant_id="t1", user_id="u1", week_start="2026-04-20")
    assert n == 1
    assert session.query(MenuPlan).count() == 0
    # Cascade: items gone too
    assert session.query(MenuPlanItem).count() == 0


def test_get_plan_for_week_returns_items_with_recipes(session):
    recipe_svc = HousewifeRecipeService(session)
    r = recipe_svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[{"title": "свёкла"}],
        source="user_dictated",
    )
    svc = HousewifeMenuService(session)
    svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[
            {"day_of_week": 0, "meal_type": "lunch", "recipe_id": r.id},
        ],
    )

    session.expire_all()
    plan = svc.get_plan_for_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
    )
    assert plan is not None
    assert len(plan.items) == 1
    assert plan.items[0].recipe is not None
    assert plan.items[0].recipe.title == "Борщ"


def test_list_user_plans_reverse_chronological(session):
    svc = HousewifeMenuService(session)
    svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-13",
        cells=[{"day_of_week": 0, "meal_type": "breakfast", "free_text": "a"}],
    )
    svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[{"day_of_week": 0, "meal_type": "breakfast", "free_text": "b"}],
    )
    plans = svc.list_user_plans(tenant_id="t1", user_id="u1")
    assert [p.week_start_date for p in plans] == [
        date(2026, 4, 20), date(2026, 4, 13),
    ]


# ---------------------------------------------------------------------------
# aggregate_ingredients_for_shopping
# ---------------------------------------------------------------------------


def test_aggregate_ingredients_empty_when_no_recipes(session):
    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[
            {"day_of_week": 0, "meal_type": "breakfast", "free_text": "овсянка"},
        ],
    )
    result = svc.aggregate_ingredients_for_shopping(
        tenant_id="t1", user_id="u1", plan_id=plan.id,
    )
    assert result == []


def test_aggregate_ingredients_flattens_per_recipe(session):
    recipe_svc = HousewifeRecipeService(session)
    r1 = recipe_svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ",
        ingredients=[
            {"title": "свёкла", "quantity_text": "2 шт"},
            {"title": "капуста", "quantity_text": "300 г"},
        ],
        source="user_dictated",
    )
    r2 = recipe_svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Омлет",
        ingredients=[
            {"title": "яйца", "quantity_text": "3 шт"},
        ],
        source="user_dictated",
    )

    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[
            {"day_of_week": 0, "meal_type": "breakfast", "recipe_id": r2.id},
            {"day_of_week": 0, "meal_type": "lunch", "recipe_id": r1.id},
            {"day_of_week": 3, "meal_type": "lunch", "recipe_id": r1.id},
            {"day_of_week": 5, "meal_type": "dinner", "free_text": "пицца навынос"},
        ],
    )

    result = svc.aggregate_ingredients_for_shopping(
        tenant_id="t1", user_id="u1", plan_id=plan.id,
    )
    # recipe_ids is a set → each recipe contributes once regardless of
    # how many times it's used in the menu
    titles = {i.title for i in result}
    assert titles == {"свёкла", "капуста", "яйца"}
    # source_recipe_id set correctly
    by_title = {i.title: i for i in result}
    assert by_title["свёкла"].source_recipe_id == r1.id
    assert by_title["яйца"].source_recipe_id == r2.id


def test_aggregate_ingredients_cross_tenant_returns_empty(session):
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()

    recipe_svc = HousewifeRecipeService(session)
    r = recipe_svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Мой", ingredients=[{"title": "x"}],
        source="user_dictated",
    )
    svc = HousewifeMenuService(session)
    plan = svc.plan_week(
        tenant_id="t1", user_id="u1", week_start="2026-04-20",
        cells=[{"day_of_week": 0, "meal_type": "lunch", "recipe_id": r.id}],
    )

    # Query under the wrong tenant
    assert svc.aggregate_ingredients_for_shopping(
        tenant_id="t2", user_id="u2", plan_id=plan.id,
    ) == []
