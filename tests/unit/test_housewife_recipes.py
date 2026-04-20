"""Unit tests for HousewifeRecipeService."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife_food import Recipe, RecipeIngredient
from sreda.services.housewife_recipes import (
    HousewifeRecipeService,
    IngredientInput,
)


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
# save_recipe
# ---------------------------------------------------------------------------


def test_save_recipe_persists_recipe_and_ingredients(session):
    svc = HousewifeRecipeService(session)
    recipe = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ",
        ingredients=[
            {"title": "свёкла", "quantity_text": "2 шт"},
            {"title": "капуста", "quantity_text": "300 г"},
            {"title": "сметана", "quantity_text": None, "is_optional": True},
        ],
        instructions_md="Варить 40 минут.",
        servings=4,
        source="user_dictated",
    )
    assert recipe.id.startswith("rec_")
    assert recipe.title == "Борщ"
    assert recipe.servings == 4

    # Ingredients loaded via relationship
    session.expire_all()
    reloaded = session.query(Recipe).first()
    assert len(reloaded.ingredients) == 3
    titles = [i.title for i in reloaded.ingredients]
    assert titles == ["свёкла", "капуста", "сметана"]
    # sort_order preserved
    assert [i.sort_order for i in reloaded.ingredients] == [0, 1, 2]
    # is_optional honoured
    assert reloaded.ingredients[2].is_optional is True


def test_save_recipe_accepts_dataclass_ingredients(session):
    svc = HousewifeRecipeService(session)
    recipe = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Пицца",
        ingredients=[
            IngredientInput(title="тесто", quantity_text="500 г"),
            IngredientInput(title="сыр", quantity_text="200 г"),
        ],
        source="ai_generated",
    )
    assert len(recipe.ingredients) == 2


def test_save_recipe_skips_empty_ingredient_titles(session):
    svc = HousewifeRecipeService(session)
    recipe = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Омлет",
        ingredients=[{"title": ""}, {"title": "  "}, {"title": "яйца"}],
        source="user_dictated",
    )
    assert len(recipe.ingredients) == 1
    assert recipe.ingredients[0].title == "яйца"


def test_save_recipe_requires_title(session):
    svc = HousewifeRecipeService(session)
    with pytest.raises(ValueError, match="title"):
        svc.save_recipe(
            tenant_id="t1", user_id="u1",
            title="",
            ingredients=[{"title": "a"}],
            source="user_dictated",
        )


def test_save_recipe_rejects_unknown_source(session):
    svc = HousewifeRecipeService(session)
    with pytest.raises(ValueError, match="source"):
        svc.save_recipe(
            tenant_id="t1", user_id="u1",
            title="Х",
            ingredients=[],
            source="mystery",
        )


def test_save_recipe_stores_tags_as_json(session):
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Оливье",
        ingredients=[{"title": "колбаса"}],
        source="user_dictated",
        tags=["салат", "праздник"],
    )
    row = session.query(Recipe).first()
    assert row.tags_json == json.dumps(["салат", "праздник"], ensure_ascii=False)


def test_save_recipe_source_url_stored(session):
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Карбонара",
        ingredients=[{"title": "паста"}],
        source="web_found",
        source_url="https://example.com/carbonara",
    )
    row = session.query(Recipe).first()
    assert row.source == "web_found"
    assert row.source_url == "https://example.com/carbonara"


def test_saved_recipe_title_is_encrypted_at_rest(session):
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="секретный рецепт",
        ingredients=[{"title": "тайный ингредиент"}],
        source="user_dictated",
    )
    raw_title = session.execute(text("SELECT title FROM recipes")).scalar()
    raw_ing = session.execute(text("SELECT title FROM recipe_ingredients")).scalar()
    assert raw_title.startswith("v2:")
    assert "секретный" not in raw_title
    assert raw_ing.startswith("v2:")
    assert "тайный" not in raw_ing


# ---------------------------------------------------------------------------
# get / list / search
# ---------------------------------------------------------------------------


def test_get_recipe_returns_with_ingredients(session):
    svc = HousewifeRecipeService(session)
    recipe = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Салат Цезарь",
        ingredients=[
            {"title": "курица"},
            {"title": "салат"},
        ],
        source="user_dictated",
    )

    session.expire_all()
    fetched = svc.get_recipe(tenant_id="t1", user_id="u1", recipe_id=recipe.id)
    assert fetched is not None
    assert fetched.title == "Салат Цезарь"
    # joinedload → ingredients populated without extra query
    assert len(fetched.ingredients) == 2


def test_get_recipe_is_tenant_scoped(session):
    """Don't return a recipe that belongs to another user in the same
    tenant, nor another tenant."""
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.add(User(id="u_other", tenant_id="t1", telegram_account_id="101"))
    session.commit()

    svc = HousewifeRecipeService(session)
    mine = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Мой рецепт",
        ingredients=[{"title": "x"}],
        source="user_dictated",
    )

    assert svc.get_recipe(tenant_id="t2", user_id="u2", recipe_id=mine.id) is None
    assert svc.get_recipe(tenant_id="t1", user_id="u_other", recipe_id=mine.id) is None
    assert svc.get_recipe(tenant_id="t1", user_id="u1", recipe_id=mine.id) is not None


def test_list_recipes_empty_returns_empty(session):
    svc = HousewifeRecipeService(session)
    assert svc.list_recipes(tenant_id="t1", user_id="u1") == []


def test_list_recipes_most_recent_first(session):
    import time
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Первый",
        ingredients=[{"title": "a"}], source="user_dictated",
    )
    time.sleep(0.01)
    svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Второй",
        ingredients=[{"title": "b"}], source="user_dictated",
    )
    rows = svc.list_recipes(tenant_id="t1", user_id="u1")
    assert [r.title for r in rows] == ["Второй", "Первый"]


def test_search_recipes_by_title_substring_case_insensitive(session):
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ украинский",
        ingredients=[], source="user_dictated",
    )
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Плов с курицей",
        ingredients=[], source="user_dictated",
    )
    result = svc.search_recipes(tenant_id="t1", user_id="u1", query="борщ")
    assert len(result) == 1
    assert result[0].title == "Борщ украинский"


def test_search_recipes_by_tag(session):
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Тирамису",
        ingredients=[], source="user_dictated",
        tags=["десерт", "итальянская"],
    )
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Суп-пюре из тыквы",
        ingredients=[], source="ai_generated",
        tags=["суп", "осенняя"],
    )
    result = svc.search_recipes(tenant_id="t1", user_id="u1", query="итальянская")
    assert [r.title for r in result] == ["Тирамису"]


def test_count_recipes_scoped_by_user(session):
    session.add(User(id="u_other", tenant_id="t1", telegram_account_id="101"))
    session.commit()

    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Мой 1",
        ingredients=[], source="user_dictated",
    )
    svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Мой 2",
        ingredients=[], source="user_dictated",
    )
    svc.save_recipe(
        tenant_id="t1", user_id="u_other", title="Чужой",
        ingredients=[], source="user_dictated",
    )

    assert svc.count_recipes(tenant_id="t1", user_id="u1") == 2
    assert svc.count_recipes(tenant_id="t1", user_id="u_other") == 1


# ---------------------------------------------------------------------------
# delete_recipe
# ---------------------------------------------------------------------------


def test_delete_recipe_cascades_ingredients(session):
    svc = HousewifeRecipeService(session)
    recipe = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="X",
        ingredients=[{"title": "a"}, {"title": "b"}],
        source="user_dictated",
    )
    assert session.query(RecipeIngredient).count() == 2

    assert svc.delete_recipe(
        tenant_id="t1", user_id="u1", recipe_id=recipe.id
    ) is True

    assert session.query(Recipe).count() == 0
    assert session.query(RecipeIngredient).count() == 0


def test_delete_recipe_returns_false_for_cross_tenant(session):
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()

    svc = HousewifeRecipeService(session)
    mine = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Мой",
        ingredients=[{"title": "x"}], source="user_dictated",
    )

    assert svc.delete_recipe(
        tenant_id="t2", user_id="u2", recipe_id=mine.id
    ) is False
    # Still there
    assert session.query(Recipe).count() == 1


def test_delete_recipe_returns_false_for_unknown_id(session):
    svc = HousewifeRecipeService(session)
    assert svc.delete_recipe(
        tenant_id="t1", user_id="u1", recipe_id="rec_bogus"
    ) is False
