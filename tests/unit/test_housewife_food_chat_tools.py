"""Integration tests for the 5 shopping LLM tools in
``build_housewife_tools``. Invokes the LangChain tool objects directly
(not via FakeLLM) since that's more deterministic and the routing
behaviour is covered in existing ``test_conversation_chat`` flows."""

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


def _tools_by_name(session):
    return {
        t.name: t
        for t in build_housewife_tools(
            session=session, tenant_id="t1", user_id="u1"
        )
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_shopping_tools_are_registered(session):
    names = set(_tools_by_name(session).keys())
    assert {
        "add_shopping_items",
        "mark_shopping_bought",
        "remove_shopping_items",
        "list_shopping",
        "clear_bought_shopping",
    } <= names


# ---------------------------------------------------------------------------
# add_shopping_items
# ---------------------------------------------------------------------------


def test_add_shopping_items_creates_rows(session):
    tools = _tools_by_name(session)
    result = tools["add_shopping_items"].invoke({
        "items": [
            {"title": "молоко", "category": "молочные"},
            {"title": "хлеб", "category": "хлеб"},
        ]
    })
    assert result.startswith("ok:added:2")
    # ids included in result
    assert "ids=[sh_" in result

    rows = session.query(ShoppingListItem).all()
    assert len(rows) == 2
    assert {r.title for r in rows} == {"молоко", "хлеб"}


def test_add_shopping_items_empty_list_errors(session):
    tools = _tools_by_name(session)
    result = tools["add_shopping_items"].invoke({"items": []})
    assert result.startswith("error:")


def test_add_shopping_items_skips_empty_titles(session):
    tools = _tools_by_name(session)
    result = tools["add_shopping_items"].invoke({
        "items": [{"title": ""}, {"title": "хлеб"}]
    })
    # Only one actually inserted despite 2 in the payload
    assert result.startswith("ok:added:1")
    assert session.query(ShoppingListItem).count() == 1


# ---------------------------------------------------------------------------
# list_shopping
# ---------------------------------------------------------------------------


def test_list_shopping_empty(session):
    tools = _tools_by_name(session)
    assert tools["list_shopping"].invoke({}) == "no shopping items"


def test_list_shopping_groups_by_category(session):
    tools = _tools_by_name(session)
    tools["add_shopping_items"].invoke({
        "items": [
            {"title": "молоко", "category": "молочные"},
            {"title": "хлеб", "category": "хлеб"},
            {"title": "сыр", "category": "молочные"},
        ]
    })
    result = tools["list_shopping"].invoke({})
    # One group header per category
    assert result.count("[молочные]") == 1
    assert result.count("[хлеб]") == 1
    # All three items present
    assert "молоко" in result
    assert "сыр" in result
    assert "хлеб" in result
    # Each item has an id reference
    assert "[sh_" in result


def test_list_shopping_omits_bought_and_cancelled(session):
    tools = _tools_by_name(session)
    result = tools["add_shopping_items"].invoke({
        "items": [
            {"title": "молоко"},
            {"title": "хлеб"},
        ]
    })
    # Pull ids back out of the ok:... string
    ids = [
        s for s in
        result.split("ids=[")[1].rstrip("]").split(",")
    ]
    tools["mark_shopping_bought"].invoke({"item_ids": [ids[0]]})
    listing = tools["list_shopping"].invoke({})
    # "молоко" bought → gone; "хлеб" pending → shown
    assert "молоко" not in listing
    assert "хлеб" in listing


# ---------------------------------------------------------------------------
# mark_shopping_bought / remove_shopping_items
# ---------------------------------------------------------------------------


def test_mark_shopping_bought_updates_status(session):
    tools = _tools_by_name(session)
    result = tools["add_shopping_items"].invoke({
        "items": [{"title": "молоко"}, {"title": "хлеб"}]
    })
    ids = result.split("ids=[")[1].rstrip("]").split(",")

    out = tools["mark_shopping_bought"].invoke({"item_ids": ids})
    assert out == "ok:bought:2"
    for item_id in ids:
        assert session.get(ShoppingListItem, item_id).status == "bought"


def test_remove_shopping_items_cancels(session):
    tools = _tools_by_name(session)
    result = tools["add_shopping_items"].invoke({
        "items": [{"title": "молоко"}]
    })
    item_id = result.split("ids=[")[1].rstrip("]")

    out = tools["remove_shopping_items"].invoke({"item_ids": [item_id]})
    assert out == "ok:removed:1"
    assert session.get(ShoppingListItem, item_id).status == "cancelled"


def test_mark_shopping_bought_empty_errors(session):
    tools = _tools_by_name(session)
    assert tools["mark_shopping_bought"].invoke({"item_ids": []}).startswith("error:")


# ---------------------------------------------------------------------------
# clear_bought_shopping
# ---------------------------------------------------------------------------


def test_clear_bought_moves_to_cancelled(session):
    tools = _tools_by_name(session)
    result = tools["add_shopping_items"].invoke({
        "items": [{"title": "молоко"}, {"title": "хлеб"}, {"title": "сыр"}]
    })
    ids = result.split("ids=[")[1].rstrip("]").split(",")

    # Mark first two bought, third stays pending
    tools["mark_shopping_bought"].invoke({"item_ids": ids[:2]})

    out = tools["clear_bought_shopping"].invoke({})
    assert out == "ok:cleared:2"

    statuses = {
        r.id: r.status
        for r in session.query(ShoppingListItem).all()
    }
    assert statuses[ids[0]] == "cancelled"
    assert statuses[ids[1]] == "cancelled"
    assert statuses[ids[2]] == "pending"


# ---------------------------------------------------------------------------
# Missing user_id context
# ---------------------------------------------------------------------------


def test_tools_error_when_user_id_missing(session):
    tools_no_user = {
        t.name: t
        for t in build_housewife_tools(
            session=session, tenant_id="t1", user_id=None
        )
    }
    assert tools_no_user["list_shopping"].invoke({}).startswith("error:")
    assert tools_no_user["add_shopping_items"].invoke(
        {"items": [{"title": "x"}]}
    ).startswith("error:")
