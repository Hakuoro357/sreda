"""Tests for cooking_time_minutes field on Recipe.

Phase 7 (2026-04-28). Single int field, semantics: общее время от
нарезки до подачи на стол. nullable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife_food import Recipe
from sreda.services.housewife_recipes import HousewifeRecipeService as RecipeService


def _setup():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(Tenant(id="t1", name="Test"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    s.commit()
    return s


def test_save_recipe_with_cooking_time():
    s = _setup()
    svc = RecipeService(s)
    recipe, is_new = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Борщ",
        ingredients=[],
        cooking_time_minutes=90,
        source="user_dictated",
    )
    assert is_new
    assert recipe.cooking_time_minutes == 90

    # Round-trip via fresh query
    s.expire_all()
    fetched = s.query(Recipe).filter_by(id=recipe.id).one()
    assert fetched.cooking_time_minutes == 90


def test_save_recipe_without_cooking_time_stays_null():
    s = _setup()
    svc = RecipeService(s)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1",
        title="Простой бутерброд",
        ingredients=[],
        source="user_dictated",
    )
    assert recipe.cooking_time_minutes is None


def test_save_recipe_caps_invalid_cooking_time_to_null():
    s = _setup()
    svc = RecipeService(s)
    # Too large — clamp to None
    r1, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Recipe1",
        ingredients=[], source="ai_generated",
        cooking_time_minutes=999999,
    )
    assert r1.cooking_time_minutes is None

    # Zero — invalid
    r2, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Recipe2",
        ingredients=[], source="ai_generated",
        cooking_time_minutes=0,
    )
    assert r2.cooking_time_minutes is None

    # Negative — invalid
    r3, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Recipe3",
        ingredients=[], source="ai_generated",
        cooking_time_minutes=-10,
    )
    assert r3.cooking_time_minutes is None

    # Non-numeric — graceful
    r4, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Recipe4",
        ingredients=[], source="ai_generated",
        cooking_time_minutes="приблизительно час",  # type: ignore
    )
    assert r4.cooking_time_minutes is None


def test_save_recipes_batch_with_cooking_time():
    s = _setup()
    svc = RecipeService(s)
    result = svc.save_recipes_batch(
        tenant_id="t1", user_id="u1",
        recipes=[
            {
                "title": "Плов",
                "source": "user_dictated",
                "cooking_time_minutes": 75,
            },
            {
                "title": "Салат",
                "source": "user_dictated",
                "cooking_time_minutes": 10,
            },
            {
                # Without cooking_time — should stay None
                "title": "Загадочное блюдо",
                "source": "user_dictated",
            },
        ],
    )
    assert len(result.created) == 3
    by_title = {r.title: r for r in result.created}
    assert by_title["Плов"].cooking_time_minutes == 75
    assert by_title["Салат"].cooking_time_minutes == 10
    assert by_title["Загадочное блюдо"].cooking_time_minutes is None


def test_save_recipes_batch_invalid_cooking_time_becomes_null():
    s = _setup()
    svc = RecipeService(s)
    result = svc.save_recipes_batch(
        tenant_id="t1", user_id="u1",
        recipes=[
            {
                "title": "Бесконечный суп",
                "source": "user_dictated",
                "cooking_time_minutes": 99999,
            },
            {
                "title": "Bad type",
                "source": "user_dictated",
                "cooking_time_minutes": "час",
            },
        ],
    )
    assert len(result.created) == 2
    for r in result.created:
        assert r.cooking_time_minutes is None
