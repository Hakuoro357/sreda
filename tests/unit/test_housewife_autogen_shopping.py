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


def _tools(session, *, menu_display_state=None):
    return {
        t.name: t
        for t in build_housewife_tools(
            session=session,
            tenant_id="t1",
            user_id="u1",
            menu_display_state=menu_display_state,
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

    # 3. Generate shopping. As of v1.2 Stage 8 this goes through an
    # LLM transformer (convert_ingredients_to_shopping_list). In unit
    # tests there's no LLM configured, so we stub the transformer to
    # just pass the ingredients through with default category. The
    # point of THIS test is the happy-path wiring, not the LLM output.
    import sreda.services.housewife_shopping_llm as _llm_mod

    original_convert = _llm_mod.convert_ingredients_to_shopping_list
    def _passthrough(ingredients, *, eaters_count, llm=None):
        return [
            {
                "title": i.title,
                "quantity_text": i.quantity_text,
                "category": None,
                "source_recipe_id": i.source_recipe_id,
            }
            for i in ingredients
        ]
    _llm_mod.convert_ingredients_to_shopping_list = _passthrough
    try:
        gen_result = tools["generate_shopping_from_menu"].invoke({
            "plan_id": plan_id,
        })
    finally:
        _llm_mod.convert_ingredients_to_shopping_list = original_convert

    # v1.2 Stage 4 — return format now includes eaters=E. With no
    # family members recorded, count_eaters falls back to 1 (solo user).
    assert gen_result == "ok:generated:3:eaters=1"

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


def test_list_menu_returns_llm_readable_day_blocks(session):
    menu_display_state = {}
    tools = _tools(session, menu_display_state=menu_display_state)

    recipe_result = tools["save_recipe"].invoke({
        "title": "Омлет",
        "ingredients": [{"title": "яйца", "quantity_text": "3 шт"}],
        "instructions_md": "Жарить 5 минут",
        "servings": 2,
        "source": "user_dictated",
    })
    recipe_id = recipe_result.split(":")[-1]

    plan = tools["plan_week_menu"].invoke({
        "week_start": "2026-04-20",
        "days": [
            {
                "day_of_week": 0,
                "meals": {
                    "breakfast": {"free_text": "овсянка"},
                    "lunch": {"free_text": "суп"},
                    "dinner": {"free_text": "плов"},
                },
            },
            {
                "day_of_week": 1,
                "meals": {
                    "breakfast": {"recipe_id": recipe_id},
                    "lunch": {"free_text": "борщ"},
                    "dinner": {"free_text": "рыба"},
                },
            },
        ],
    })
    assert plan.startswith("ok:plan_created:")

    result = tools["list_menu"].invoke({"week_start": "2026-04-20"})

    assert "menu_id:" in result
    assert "week_start: 2026-04-20" in result
    assert "Понедельник, 20 апреля" in result
    assert "Вторник, 21 апреля" in result
    assert "\n\nВторник, 21 апреля\n" in result
    assert f"[{recipe_id}] Омлет" in result
    assert result.index("Завтрак: овсянка") < result.index("Обед: суп")
    assert result.index("Обед: суп") < result.index("Ужин: плов")
    assert "  пн:" not in result
    assert "breakfast:" not in result

    user_text = menu_display_state["last_menu_reply_text"]
    assert menu_display_state["list_menu_calls"] == 1
    assert user_text.startswith("Меню на неделю 20–26 апреля:\n\n")
    assert "\n\nВторник, 21 апреля\n" in user_text
    assert "• Завтрак: Омлет" in user_text
    assert f"[{recipe_id}]" not in user_text
    assert "menu_id:" not in user_text


def test_generate_shopping_from_menu_free_text_only_yields_plan_no_recipes(session):
    """Codex Sub-A4 menu R3/R4 (gen_shopping split): plan exists but
    every cell is free_text — no recipe_id'd items to extract from.
    Pre-R3 the tool returned ``ok:generated:0`` (indistinguishable
    from unknown plan and from empty-conversion). R3 split this to
    a dedicated status so the composer can say «у этого меню нет
    сохранённых рецептов» rather than the misleading «покупок нет».
    """
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
    assert result == "ok:plan_no_recipes", (
        f"free-text-only plan must produce ok:plan_no_recipes "
        f"(distinct from empty-conversion + plan_not_found), got {result!r}"
    )
    assert session.query(ShoppingListItem).count() == 0


def test_generate_shopping_from_menu_unknown_plan_id_yields_plan_not_found(session):
    """Codex Sub-A4 menu R3/R4 (gen_shopping split): unknown
    plan_id used to be silently mapped to ``ok:generated:0``,
    leaving the user with the misleading «покупок нет». R3 split
    this to ``error:plan_not_found`` so the composer can say «не
    нашла такого меню» explicitly."""
    tools = _tools(session)
    result = tools["generate_shopping_from_menu"].invoke({
        "plan_id": "menu_bogus",
    })
    assert result == "error:plan_not_found", (
        f"unknown plan_id must produce error:plan_not_found "
        f"(distinct from any ok:generated:* shape), got {result!r}"
    )


def test_generate_shopping_from_menu_recipe_cells_empty_conversion_yields_zero_eaters(session):
    """Codex Sub-A4 menu R4 (gen_shopping split): the third
    distinguishable shape — plan has recipe_id cells (so it's NOT
    plan_no_recipes), but the LLM converter drops everything
    (e.g. all «по вкусу»). Runtime returns ``ok:generated:0:eaters=E``
    — eaters is known because the recipe-cells branch was taken
    and family-size was queried. Distinct from
    ``ok:plan_no_recipes`` and ``error:plan_not_found``."""
    tools = _tools(session)

    # Save a recipe with shoppable ingredients
    r1 = tools["save_recipe"].invoke({
        "title": "Овсянка",
        "ingredients": [{"title": "овсянка", "quantity_text": "100 г"}],
        "instructions_md": "Варить 10 минут",
        "servings": 1,
        "source": "user_dictated",
    })
    r1_id = r1.split(":")[-1]

    plan = tools["plan_week_menu"].invoke({
        "week_start": "2026-04-20",
        "days": [
            {
                "day_of_week": 0,
                "meals": {"breakfast": {"recipe_id": r1_id}},
            }
        ],
    })
    plan_id = plan.split(":")[2]

    # Stub the LLM converter to return EMPTY list — simulates the
    # «recipes exist but converter dropped everything» path.
    import sreda.services.housewife_shopping_llm as _llm_mod
    original_convert = _llm_mod.convert_ingredients_to_shopping_list
    _llm_mod.convert_ingredients_to_shopping_list = (
        lambda ingredients, *, eaters_count, llm=None: []
    )
    try:
        result = tools["generate_shopping_from_menu"].invoke({
            "plan_id": plan_id,
        })
    finally:
        _llm_mod.convert_ingredients_to_shopping_list = original_convert

    # eaters=1 because no family members recorded (count_eaters
    # fallback). The presence of :eaters=1 is what distinguishes
    # this from plan_no_recipes (no eaters segment) and from
    # plan_not_found (different prefix entirely).
    assert result == "ok:generated:0:eaters=1", (
        f"recipe-cells + empty-conversion must produce "
        f"ok:generated:0:eaters=E (not ok:plan_no_recipes — recipes "
        f"DID exist), got {result!r}"
    )
    assert session.query(ShoppingListItem).count() == 0


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
