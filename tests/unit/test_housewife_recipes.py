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
    recipe, _ = svc.save_recipe(
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
    recipe, _ = svc.save_recipe(
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
    recipe, _ = svc.save_recipe(
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
    recipe, _ = svc.save_recipe(
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
    mine, _ = svc.save_recipe(
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
    recipe, _ = svc.save_recipe(
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
    mine, _ = svc.save_recipe(
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


# ---------------------------------------------------------------------------
# save_recipes_batch — added to unblock "сохрани 18 рецептов" workflows
# ---------------------------------------------------------------------------


def test_save_recipes_batch_persists_all(session):
    svc = HousewifeRecipeService(session)
    result = svc.save_recipes_batch(
        tenant_id="t1", user_id="u1",
        recipes=[
            {
                "title": "Борщ",
                "ingredients": [{"title": "свёкла"}, {"title": "капуста"}],
                "instructions_md": "варить 40 мин",
                "servings": 4,
                "source": "ai_generated",
            },
            {
                "title": "Омлет",
                "ingredients": [{"title": "яйца", "quantity_text": "3 шт"}],
                "instructions_md": "жарить 5 мин",
                "servings": 2,
                "source": "ai_generated",
            },
            {
                "title": "Паста",
                "ingredients": [{"title": "паста"}, {"title": "сыр"}],
                "instructions_md": "варить 10 мин",
                "servings": 2,
                "source": "ai_generated",
            },
        ],
    )
    assert len(result.created) == 3
    assert result.skipped_existing == []
    titles = {r.title for r in result.created}
    assert titles == {"Борщ", "Омлет", "Паста"}

    # Ingredients persisted
    assert session.query(RecipeIngredient).count() == 5


def test_save_recipes_batch_skips_invalid_items(session):
    """A bad entry shouldn't nuke the whole batch."""
    svc = HousewifeRecipeService(session)
    result = svc.save_recipes_batch(
        tenant_id="t1", user_id="u1",
        recipes=[
            {"title": "Борщ", "ingredients": [{"title": "x"}], "source": "user_dictated"},
            {"title": "", "ingredients": [], "source": "user_dictated"},     # empty title
            {"title": "Bad source", "ingredients": [], "source": "mystery"}, # invalid source
            "not a dict",                                                     # wrong shape
            {"title": "Омлет", "ingredients": [], "source": "ai_generated"},
        ],
    )
    assert len(result.created) == 2
    titles = {r.title for r in result.created}
    assert titles == {"Борщ", "Омлет"}


def test_save_recipes_batch_tags_json_encoded(session):
    svc = HousewifeRecipeService(session)
    result = svc.save_recipes_batch(
        tenant_id="t1", user_id="u1",
        recipes=[
            {
                "title": "X",
                "ingredients": [],
                "source": "user_dictated",
                "tags": ["суп", "быстрое"],
            },
        ],
    )
    assert result.created[0].tags_json == json.dumps(
        ["суп", "быстрое"], ensure_ascii=False
    )


def test_save_recipes_batch_empty_list_returns_empty(session):
    svc = HousewifeRecipeService(session)
    result = svc.save_recipes_batch(
        tenant_id="t1", user_id="u1", recipes=[]
    )
    assert result.created == []
    assert result.skipped_existing == []


# ---------------------------------------------------------------------------
# Dedup by title (Stage 6, v1.2)
# ---------------------------------------------------------------------------


def test_save_recipe_returns_existing_on_duplicate_title(session):
    """Second save_recipe with same (tenant, user, normalised-title)
    must NOT insert a new row — return the original with is_new=False."""
    svc = HousewifeRecipeService(session)
    first, is_new1 = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[{"title": "свёкла"}],
        source="user_dictated",
    )
    assert is_new1 is True

    second, is_new2 = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ",  # exact same title
        ingredients=[{"title": "капуста"}],  # different ingredients
        source="ai_generated",
    )
    assert is_new2 is False
    assert second.id == first.id
    # Only ONE row in DB despite two calls
    assert svc.count_recipes(tenant_id="t1", user_id="u1") == 1


def test_save_recipe_title_dedup_is_case_and_whitespace_insensitive(session):
    """'Борщ' and '  борщ ' normalise to the same key."""
    svc = HousewifeRecipeService(session)
    first, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[{"title": "x"}],
        source="user_dictated",
    )
    _, is_new = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="  борщ  ",  # case + padding
        ingredients=[{"title": "y"}],
        source="user_dictated",
    )
    assert is_new is False
    assert svc.count_recipes(tenant_id="t1", user_id="u1") == 1


def test_save_recipe_same_title_different_user_not_duplicate(session):
    """Dedup is per (tenant, user). Another user can have 'Борщ' too."""
    session.add(User(id="u2", tenant_id="t1", telegram_account_id="200"))
    session.commit()

    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[], source="user_dictated",
    )
    _, is_new = svc.save_recipe(
        tenant_id="t1", user_id="u2",
        title="Борщ", ingredients=[], source="user_dictated",
    )
    assert is_new is True  # different user → not a dup
    assert svc.count_recipes(tenant_id="t1", user_id="u1") == 1
    assert svc.count_recipes(tenant_id="t1", user_id="u2") == 1


def test_save_recipes_batch_dedups_within_input(session):
    """LLM passes the same title twice in one batch — collapse to one."""
    svc = HousewifeRecipeService(session)
    result = svc.save_recipes_batch(
        tenant_id="t1", user_id="u1",
        recipes=[
            {"title": "Плов", "ingredients": [], "source": "user_dictated"},
            {"title": "плов", "ingredients": [], "source": "user_dictated"},
            {"title": "Омлет", "ingredients": [], "source": "user_dictated"},
        ],
    )
    assert len(result.created) == 2  # плов collapsed, Омлет new
    titles = {r.title for r in result.created}
    assert titles == {"Плов", "Омлет"}


def test_are_titles_similar_subset():
    """'Борщ' is a subset of 'Борщ классический' → same dish."""
    from sreda.services.housewife_recipes import _are_titles_similar
    assert _are_titles_similar("Борщ", "Борщ классический")
    assert _are_titles_similar("Пельмени", "Пельмени со сметаной")
    # Symmetric
    assert _are_titles_similar("Пельмени со сметаной", "Пельмени")


def test_are_titles_similar_jaccard_half_tokens_shared():
    """'Плов с курицей на 5 чел' vs 'Плов с курицей на 6 чел' —
    differ only in one word. 5/7 tokens shared → merge."""
    from sreda.services.housewife_recipes import _are_titles_similar
    assert _are_titles_similar(
        "Плов с курицей на 5 чел",
        "Плов с курицей на 6 чел",
    )


def test_are_titles_similar_different_dishes():
    """'Борщ' and 'Щи' share no tokens → keep separate."""
    from sreda.services.housewife_recipes import _are_titles_similar
    assert not _are_titles_similar("Борщ", "Щи")
    assert not _are_titles_similar("Оливье", "Цезарь")


def test_are_titles_similar_cooking_method_differs():
    """Cooking method matters — 'Жареная картошка' vs 'Тушёная картошка'
    share only 'картошка' (1/3 Jaccard) → different recipes."""
    from sreda.services.housewife_recipes import _are_titles_similar
    assert not _are_titles_similar("Жареная картошка", "Тушёная картошка")


def test_are_titles_similar_case_insensitive():
    from sreda.services.housewife_recipes import _are_titles_similar
    assert _are_titles_similar("БОРЩ", "борщ классический")


def test_save_recipe_fuzzy_dedup_suppresses_variation(session):
    """The whole point of fuzzy-match: don't save 'Пельмени со сметаной'
    when 'Пельмени' already exists. Return existing, is_new=False."""
    svc = HousewifeRecipeService(session)
    first, is_new1 = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Пельмени", ingredients=[{"title": "тесто"}],
        source="user_dictated",
    )
    assert is_new1 is True

    second, is_new2 = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Пельмени со сметаной",  # same dish + variation
        ingredients=[{"title": "тесто"}, {"title": "сметана"}],
        source="user_dictated",
    )
    assert is_new2 is False
    assert second.id == first.id
    assert svc.count_recipes(tenant_id="t1", user_id="u1") == 1


def test_save_recipe_fuzzy_dedup_allows_distinct_dishes(session):
    """Safety: 'Жареная картошка' and 'Тушёная картошка' differ in
    cooking method — MUST stay as separate recipes, not merge."""
    svc = HousewifeRecipeService(session)
    _, n1 = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Жареная картошка", ingredients=[{"title": "картошка"}],
        source="user_dictated",
    )
    _, n2 = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Тушёная картошка", ingredients=[{"title": "картошка"}],
        source="user_dictated",
    )
    assert n1 is True and n2 is True
    assert svc.count_recipes(tenant_id="t1", user_id="u1") == 2


def test_save_recipes_batch_reports_skipped_against_db(session):
    """Title already in book → skipped_existing, not re-inserted."""
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[], source="user_dictated",
    )
    result = svc.save_recipes_batch(
        tenant_id="t1", user_id="u1",
        recipes=[
            {"title": "Борщ", "ingredients": [], "source": "user_dictated"},
            {"title": "Окрошка", "ingredients": [], "source": "user_dictated"},
        ],
    )
    assert len(result.created) == 1
    assert result.created[0].title == "Окрошка"
    assert len(result.skipped_existing) == 1
    assert result.skipped_existing[0].title == "Борщ"
    assert svc.count_recipes(tenant_id="t1", user_id="u1") == 2


# ---------------------------------------------------------------------------
# Per-instance list cache (Stage 7.5 NICE). One LLM turn re-uses the
# same HousewifeRecipeService — when it calls list_recipes("")  /
# search_recipes() multiple times (common: before dedup, before render,
# before confirmation message), we should hit the DB once. Invalidated
# on save_recipe / save_recipes_batch / delete_recipe.
# ---------------------------------------------------------------------------


def test_list_recipes_caches_empty_query_on_instance(session):
    """Second call with no query reuses the cached row list without
    hitting the DB again — LLM often calls search_recipes() twice in
    one turn and each call decrypts every title."""
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[], source="user_dictated",
    )

    calls = {"n": 0}
    real_loader = svc._load_owned_recipes

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real_loader(*args, **kwargs)

    svc._load_owned_recipes = counting  # type: ignore[method-assign]

    a = svc.list_recipes(tenant_id="t1", user_id="u1")
    b = svc.list_recipes(tenant_id="t1", user_id="u1")
    c = svc.search_recipes(tenant_id="t1", user_id="u1", query="")
    assert [r.title for r in a] == ["Борщ"]
    assert [r.title for r in b] == ["Борщ"]
    assert [r.title for r in c] == ["Борщ"]
    assert calls["n"] == 1, (
        "list_recipes(query=None/'') should DB-load once per instance, "
        "not on every call"
    )


def test_list_recipes_with_query_does_not_poison_cache(session):
    """Filtered calls must not cache narrowed results under the empty-
    query key — otherwise a later list_recipes() would miss rows."""
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[], source="user_dictated",
    )
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Окрошка", ingredients=[], source="user_dictated",
    )

    filtered = svc.list_recipes(tenant_id="t1", user_id="u1", query="борщ")
    assert [r.title for r in filtered] == ["Борщ"]

    full = svc.list_recipes(tenant_id="t1", user_id="u1")
    assert {r.title for r in full} == {"Борщ", "Окрошка"}


def test_save_recipe_invalidates_list_cache(session):
    """After saving, the cached list must reflect the new row on next
    call — otherwise the LLM would report 'у тебя 5 рецептов' right
    after saving the 6th."""
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[], source="user_dictated",
    )
    first = svc.list_recipes(tenant_id="t1", user_id="u1")
    assert len(first) == 1

    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Окрошка", ingredients=[], source="user_dictated",
    )
    second = svc.list_recipes(tenant_id="t1", user_id="u1")
    assert {r.title for r in second} == {"Борщ", "Окрошка"}


def test_delete_recipe_invalidates_list_cache(session):
    svc = HousewifeRecipeService(session)
    r1, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[], source="user_dictated",
    )
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Окрошка", ingredients=[], source="user_dictated",
    )
    # prime cache
    assert len(svc.list_recipes(tenant_id="t1", user_id="u1")) == 2

    svc.delete_recipe(tenant_id="t1", user_id="u1", recipe_id=r1.id)
    assert [r.title for r in svc.list_recipes(tenant_id="t1", user_id="u1")] == ["Окрошка"]


def test_save_recipes_batch_invalidates_list_cache(session):
    svc = HousewifeRecipeService(session)
    svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ", ingredients=[], source="user_dictated",
    )
    assert len(svc.list_recipes(tenant_id="t1", user_id="u1")) == 1

    svc.save_recipes_batch(
        tenant_id="t1", user_id="u1",
        recipes=[
            {"title": "Окрошка", "ingredients": [], "source": "user_dictated"},
            {"title": "Омлет", "ingredients": [], "source": "user_dictated"},
        ],
    )
    titles = {r.title for r in svc.list_recipes(tenant_id="t1", user_id="u1")}
    assert titles == {"Борщ", "Окрошка", "Омлет"}
