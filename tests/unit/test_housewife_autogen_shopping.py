"""End-to-end test for generate_shopping_from_menu.

Exercises the full pipeline: save recipes → plan a week → generate
shopping list → inspect shopping_list_items rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife_food import ShoppingListItem
from sreda.services.housewife_chat_tools import build_housewife_tools


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


def _tools(session):
    return {
        t.name: t
        for t in build_housewife_tools(
            session=session, tenant_id="t1", user_id="u1"
        )
    }


def test_generate_shopping_from_menu_end_to_end(session):
    tools = _tools(session)

    # 1. Save two recipes
    r1 = tools["save_recipe"].invoke({
        "title": "Борщ",
        "ingredients": [
            {"title": "свёкла", "quantity_text": "2 шт"},
            {"title": "капуста", "quantity_text": "300 г"},
        ],
        "instructions_md": "Варить 40 минут",
        "servings": 4,
        "source": "user_dictated",
    })
    r1_id = r1.split(":")[-1]

    r2 = tools["save_recipe"].invoke({
        "title": "Омлет",
        "ingredients": [{"title": "яйца", "quantity_text": "3 шт"}],
        "instructions_md": "Жарить 5 минут",
        "servings": 2,
        "source": "ai_generated",
    })
    r2_id = r2.split(":")[-1]

    # 2. Plan a week referencing both
    plan_result = tools["plan_week_menu"].invoke({
        "week_start": "2026-04-20",
        "days": [
            {
                "day_of_week": 0,
                "meals": {
                    "breakfast": {"recipe_id": r2_id},
                    "lunch": {"recipe_id": r1_id},
                    "dinner": {"free_text": "пицца навынос"},
                }
            },
            {
                "day_of_week": 1,
                "meals": {
                    "breakfast": {"recipe_id": r2_id},  # same recipe again
                }
            }
        ],
    })
    plan_id = plan_result.split(":")[2]

    # 3. Generate shopping — expect 3 ingredients total (Борщ: 2,
    # Омлет: 1; deduplicated by recipe, not by ingredient, so no
    # double-counting of яйца despite being used twice in the week)
    gen_result = tools["generate_shopping_from_menu"].invoke({
        "plan_id": plan_id,
    })
    assert gen_result == "ok:generated:3"

    # 4. Inspect shopping rows
    rows = session.query(ShoppingListItem).all()
    assert len(rows) == 3
    titles = sorted(r.title for r in rows)
    assert titles == sorted(["свёкла", "капуста", "яйца"])
    # source_recipe_id preserved
    sources = {r.title: r.source_recipe_id for r in rows}
    assert sources["свёкла"] == r1_id
    assert sources["капуста"] == r1_id
    assert sources["яйца"] == r2_id


def test_generate_shopping_from_menu_free_text_only_yields_zero(session):
    tools = _tools(session)

    plan = tools["plan_week_menu"].invoke({
        "week_start": "2026-04-20",
        "days": [
            {
                "day_of_week": 0,
                "meals": {
                    "breakfast": {"free_text": "овсянка"},
                    "lunch": {"free_text": "суп"},
                }
            }
        ],
    })
    plan_id = plan.split(":")[2]

    result = tools["generate_shopping_from_menu"].invoke({
        "plan_id": plan_id,
    })
    assert result == "ok:generated:0"
    assert session.query(ShoppingListItem).count() == 0


def test_generate_shopping_from_menu_unknown_plan_id_yields_zero(session):
    tools = _tools(session)
    result = tools["generate_shopping_from_menu"].invoke({
        "plan_id": "menu_bogus",
    })
    assert result == "ok:generated:0"


def test_generate_shopping_from_menu_missing_user_id_errors(session):
    tools_no_user = {
        t.name: t
        for t in build_housewife_tools(
            session=session, tenant_id="t1", user_id=None
        )
    }
    result = tools_no_user["generate_shopping_from_menu"].invoke({
        "plan_id": "menu_x",
    })
    assert result.startswith("error:")
