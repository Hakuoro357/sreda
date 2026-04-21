"""Unit tests for HousewifeShoppingService."""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife_food import SHOPPING_CATEGORIES, ShoppingListItem
from sreda.services.encryption import get_encryption_service
from sreda.services.housewife_shopping import (
    DEFAULT_CATEGORY,
    HousewifeShoppingService,
    ShoppingItemInput,
    _coerce_category,
)


@pytest.fixture(autouse=True)
def _stable_encryption_key(monkeypatch):
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "test")
    monkeypatch.delenv("SREDA_ENCRYPTION_KEY_SALT", raising=False)
    monkeypatch.delenv("SREDA_ENCRYPTION_LEGACY_KEYS", raising=False)
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    get_encryption_service.cache_clear()
    yield
    get_settings.cache_clear()
    get_encryption_service.cache_clear()


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
# _coerce_category
# ---------------------------------------------------------------------------


def test_coerce_category_maps_exact():
    assert _coerce_category("молочные") == "молочные"
    assert _coerce_category("Молочные") == "молочные"  # case-insensitive
    assert _coerce_category(" бакалея ") == "бакалея"  # trim


def test_coerce_category_unknown_falls_to_default():
    assert _coerce_category("специи") == DEFAULT_CATEGORY
    assert _coerce_category("") == DEFAULT_CATEGORY
    assert _coerce_category(None) == DEFAULT_CATEGORY


# ---------------------------------------------------------------------------
# add_items
# ---------------------------------------------------------------------------


def test_add_items_persists_batch(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1",
        user_id="u1",
        items=[
            {"title": "молоко", "category": "молочные"},
            {"title": "хлеб", "category": "хлеб"},
            {"title": "помидоры", "quantity_text": "1 кг", "category": "овощи_фрукты"},
        ],
    )
    assert len(rows) == 3
    # Reload from DB to confirm encryption round-trip
    session.expire_all()
    all_items = session.query(ShoppingListItem).all()
    titles = {r.title for r in all_items}
    assert titles == {"молоко", "хлеб", "помидоры"}
    # Categories recorded
    cats = {r.category for r in all_items}
    assert cats == {"молочные", "хлеб", "овощи_фрукты"}


def test_add_items_accepts_dataclass_input(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1",
        user_id="u1",
        items=[ShoppingItemInput(title="кефир", category="молочные")],
    )
    assert len(rows) == 1
    assert rows[0].title == "кефир"


def test_add_items_skips_empty_title(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1",
        user_id="u1",
        items=[{"title": ""}, {"title": "  "}, {"title": "молоко"}],
    )
    assert len(rows) == 1
    assert rows[0].title == "молоко"


def test_add_items_unknown_category_falls_to_default(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1",
        user_id="u1",
        items=[{"title": "шафран", "category": "специи"}],
    )
    assert rows[0].category == DEFAULT_CATEGORY


def test_add_items_empty_list_is_noop(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(tenant_id="t1", user_id="u1", items=[])
    assert rows == []
    assert session.query(ShoppingListItem).count() == 0


def test_title_is_encrypted_at_rest(session):
    """Sanity that EncryptedString is actually applied to title."""
    svc = HousewifeShoppingService(session)
    svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "редкое слово", "category": "бакалея"}],
    )
    from sqlalchemy import text
    raw = session.execute(text("SELECT title FROM shopping_list_items")).scalar()
    assert raw.startswith("v2:test:")
    assert "редкое слово" not in raw


# ---------------------------------------------------------------------------
# mark_bought / remove_items
# ---------------------------------------------------------------------------


def test_mark_bought_updates_status_and_skips_unknown(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "молоко"}, {"title": "хлеб"}],
    )
    a, b = rows[0].id, rows[1].id

    updated = svc.mark_bought(
        tenant_id="t1", user_id="u1",
        ids=[a, "bogus_id", b],
    )
    assert updated == 2

    statuses = {r.id: r.status for r in session.query(ShoppingListItem).all()}
    assert statuses[a] == "bought"
    assert statuses[b] == "bought"


def test_mark_bought_ignores_other_tenants(session):
    """Don't accidentally flip status for another tenant's items."""
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()

    svc = HousewifeShoppingService(session)
    mine = svc.add_items(tenant_id="t1", user_id="u1", items=[{"title": "молоко"}])
    theirs = svc.add_items(tenant_id="t2", user_id="u2", items=[{"title": "молоко"}])

    # try to mark the other tenant's item as bought from my context
    updated = svc.mark_bought(
        tenant_id="t1", user_id="u1",
        ids=[theirs[0].id],
    )
    assert updated == 0
    # Their item stays pending
    assert session.get(ShoppingListItem, theirs[0].id).status == "pending"
    # Mine untouched too (id wasn't in the list)
    assert session.get(ShoppingListItem, mine[0].id).status == "pending"


def test_remove_items_flips_to_cancelled(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "молоко"}, {"title": "хлеб"}],
    )
    svc.remove_items(tenant_id="t1", user_id="u1", ids=[rows[0].id])
    session.expire_all()
    assert session.get(ShoppingListItem, rows[0].id).status == "cancelled"
    assert session.get(ShoppingListItem, rows[1].id).status == "pending"


def test_remove_items_can_cancel_already_bought(session):
    svc = HousewifeShoppingService(session)
    [row] = svc.add_items(tenant_id="t1", user_id="u1", items=[{"title": "молоко"}])
    svc.mark_bought(tenant_id="t1", user_id="u1", ids=[row.id])
    svc.remove_items(tenant_id="t1", user_id="u1", ids=[row.id])
    assert session.get(ShoppingListItem, row.id).status == "cancelled"


# ---------------------------------------------------------------------------
# list_pending / count_pending
# ---------------------------------------------------------------------------


def test_list_pending_returns_only_pending(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[
            {"title": "молоко", "category": "молочные"},
            {"title": "хлеб", "category": "хлеб"},
            {"title": "картошка", "category": "овощи_фрукты"},
        ],
    )
    svc.mark_bought(tenant_id="t1", user_id="u1", ids=[rows[0].id])
    svc.remove_items(tenant_id="t1", user_id="u1", ids=[rows[2].id])

    pending = svc.list_pending(tenant_id="t1", user_id="u1")
    titles = [r.title for r in pending]
    assert titles == ["хлеб"]


def test_list_pending_ordered_by_taxonomy(session):
    """Rendering order should follow the fixed shopping categories list,
    not alphabetical — so the shopper sees the list in "store aisle" order.
    """
    svc = HousewifeShoppingService(session)
    # Insert in alphabetical order; expect taxonomy order on read.
    svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[
            {"title": "бакалея-item", "category": "бакалея"},
            {"title": "молочные-item", "category": "молочные"},
            {"title": "хлеб-item", "category": "хлеб"},
        ],
    )
    pending = svc.list_pending(tenant_id="t1", user_id="u1")
    cats = [r.category for r in pending]
    # Taxonomy order: молочные → мясо_рыба → овощи_фрукты → хлеб → бакалея
    idx_map = {c: i for i, c in enumerate(SHOPPING_CATEGORIES)}
    assert cats == sorted(cats, key=idx_map.get)


def test_count_pending_respects_status(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "a"}, {"title": "b"}, {"title": "c"}],
    )
    assert svc.count_pending(tenant_id="t1", user_id="u1") == 3
    svc.mark_bought(tenant_id="t1", user_id="u1", ids=[rows[0].id])
    svc.remove_items(tenant_id="t1", user_id="u1", ids=[rows[1].id])
    assert svc.count_pending(tenant_id="t1", user_id="u1") == 1


# ---------------------------------------------------------------------------
# clear_bought
# ---------------------------------------------------------------------------


def test_clear_bought_cancels_all_bought_items(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "a"}, {"title": "b"}, {"title": "c"}],
    )
    svc.mark_bought(tenant_id="t1", user_id="u1", ids=[rows[0].id, rows[1].id])

    cleared = svc.clear_bought(tenant_id="t1", user_id="u1")
    assert cleared == 2

    statuses = {r.id: r.status for r in session.query(ShoppingListItem).all()}
    assert statuses[rows[0].id] == "cancelled"
    assert statuses[rows[1].id] == "cancelled"
    assert statuses[rows[2].id] == "pending"  # was never bought


def test_clear_bought_empty_list_is_noop(session):
    svc = HousewifeShoppingService(session)
    cleared = svc.clear_bought(tenant_id="t1", user_id="u1")
    assert cleared == 0


# ---------------------------------------------------------------------------
# clear_pending — "очистить всё" button on the shopping screen
# ---------------------------------------------------------------------------


def test_clear_pending_cancels_every_pending_row(session):
    svc = HousewifeShoppingService(session)
    svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "молоко"}, {"title": "хлеб"}, {"title": "яйца"}],
    )
    cleared = svc.clear_pending(tenant_id="t1", user_id="u1")
    assert cleared == 3
    remaining_statuses = {
        r.id: r.status for r in session.query(ShoppingListItem).all()
    }
    assert all(s == "cancelled" for s in remaining_statuses.values())


def test_clear_pending_leaves_bought_and_cancelled_alone(session):
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "A"}, {"title": "B"}, {"title": "C"}],
    )
    svc.mark_bought(tenant_id="t1", user_id="u1", ids=[rows[1].id])
    svc.remove_items(tenant_id="t1", user_id="u1", ids=[rows[2].id])
    # Only one pending now (rows[0]).
    cleared = svc.clear_pending(tenant_id="t1", user_id="u1")
    assert cleared == 1
    # State after: rows[0] cancelled (was pending), rows[1] bought
    # (untouched), rows[2] cancelled (already was).
    after = {r.id: r.status for r in session.query(ShoppingListItem).all()}
    assert after[rows[0].id] == "cancelled"
    assert after[rows[1].id] == "bought"
    assert after[rows[2].id] == "cancelled"


def test_clear_pending_empty_list_is_noop(session):
    svc = HousewifeShoppingService(session)
    assert svc.clear_pending(tenant_id="t1", user_id="u1") == 0


def test_guess_category_dairy():
    from sreda.services.housewife_shopping import _guess_category
    for title in ["молоко", "Молоко 3.2%", "сметана", "творог", "кефир", "йогурт", "сыр", "масло сливочное"]:
        assert _guess_category(title) == "молочные", f"{title!r} should be молочные, got {_guess_category(title)!r}"


def test_guess_category_meat_and_fish():
    from sreda.services.housewife_shopping import _guess_category
    for title in ["курица", "куриное филе", "говядина", "свинина", "фарш", "рыба", "лосось", "сёмга"]:
        assert _guess_category(title) == "мясо_рыба", f"{title!r}"


def test_guess_category_vegetables_fruits():
    from sreda.services.housewife_shopping import _guess_category
    for title in ["морковь", "лук репчатый", "картошка", "картофель", "помидор", "огурец", "капуста", "свёкла", "чеснок", "яблоки", "банан", "лимон", "зелень", "петрушка"]:
        assert _guess_category(title) == "овощи_фрукты", f"{title!r}"


def test_guess_category_bread():
    from sreda.services.housewife_shopping import _guess_category
    for title in ["хлеб", "батон", "булка", "хлеб бородинский"]:
        assert _guess_category(title) == "хлеб", f"{title!r}"


def test_guess_category_staples():
    from sreda.services.housewife_shopping import _guess_category
    for title in ["мука", "сахар", "соль", "рис", "гречка", "макароны", "спагетти", "масло растительное"]:
        assert _guess_category(title) == "бакалея", f"{title!r}"


def test_guess_category_unknown_falls_back_to_другое():
    from sreda.services.housewife_shopping import _guess_category
    assert _guess_category("плюшевый мишка") == "другое"
    assert _guess_category("") == "другое"
    assert _guess_category("загадочный ингредиент") == "другое"


def test_add_items_auto_classifies_when_category_missing(session):
    """When auto-gen (generate_shopping_from_menu) adds ingredients
    with category=None, the service should auto-classify by title
    keywords — otherwise everything lands in 'другое' and the
    shopping screen becomes one giant section. Observed on prod:
    13 pending items all in 'другое' including молоко, хлеб, мясо."""
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[
            {"title": "молоко"},
            {"title": "хлеб"},
            {"title": "курица"},
            {"title": "огурцы"},
            {"title": "неведомая фигня"},
        ],
    )
    by_title = {r.title: r.category for r in rows}
    assert by_title["молоко"] == "молочные"
    assert by_title["хлеб"] == "хлеб"
    assert by_title["курица"] == "мясо_рыба"
    assert by_title["огурцы"] == "овощи_фрукты"
    assert by_title["неведомая фигня"] == "другое"


def test_add_items_explicit_category_beats_auto_guess(session):
    """If LLM explicitly provides a category (chat path), trust it
    over the keyword heuristic — LLM may know context the heuristic
    doesn't (e.g. 'курица' as a stuffed toy)."""
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "курица", "category": "готовое"}],
    )
    assert rows[0].category == "готовое"


def test_clear_pending_is_tenant_scoped(session):
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()

    svc = HousewifeShoppingService(session)
    svc.add_items(tenant_id="t1", user_id="u1", items=[{"title": "A"}])
    svc.add_items(tenant_id="t2", user_id="u2", items=[{"title": "B"}])
    # Clear only for t1.
    cleared = svc.clear_pending(tenant_id="t1", user_id="u1")
    assert cleared == 1
    # t2's item remains pending.
    pending_t2 = session.query(ShoppingListItem).filter_by(
        tenant_id="t2", status="pending"
    ).count()
    assert pending_t2 == 1


# ---------------------------------------------------------------------------
# delete_by_source_recipe — Stage 5/6 regen support
# ---------------------------------------------------------------------------


def test_delete_by_source_recipe_removes_only_matching_rows(session):
    """Hard-deletes pending + bought items whose source_recipe_id matches.
    Rows with other source_recipe_id (or NULL) stay untouched."""
    svc = HousewifeShoppingService(session)
    svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[
            {"title": "свёкла", "source_recipe_id": "rec_A"},
            {"title": "капуста", "source_recipe_id": "rec_A"},
            {"title": "яйца", "source_recipe_id": "rec_B"},
            {"title": "хлеб"},  # no recipe
        ],
    )
    deleted = svc.delete_by_source_recipe(
        tenant_id="t1", user_id="u1", recipe_id="rec_A"
    )
    assert deleted == 2
    remaining = {r.title for r in session.query(ShoppingListItem).all()}
    assert remaining == {"яйца", "хлеб"}


def test_delete_by_source_recipe_ignores_empty_recipe_id(session):
    """Passing an empty/None recipe_id must be a no-op — don't accidentally
    wipe every manually-added item (NULL source_recipe_id)."""
    svc = HousewifeShoppingService(session)
    svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "хлеб"}, {"title": "молоко"}],  # no source
    )
    assert svc.delete_by_source_recipe(
        tenant_id="t1", user_id="u1", recipe_id=""
    ) == 0
    assert session.query(ShoppingListItem).count() == 2


def test_delete_by_source_recipe_is_tenant_scoped(session):
    """Another tenant's items with the same recipe_id stay intact."""
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()

    svc = HousewifeShoppingService(session)
    svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "A", "source_recipe_id": "rec_shared"}],
    )
    svc.add_items(
        tenant_id="t2", user_id="u2",
        items=[{"title": "B", "source_recipe_id": "rec_shared"}],
    )
    deleted = svc.delete_by_source_recipe(
        tenant_id="t1", user_id="u1", recipe_id="rec_shared"
    )
    assert deleted == 1
    remaining_titles = {r.title for r in session.query(ShoppingListItem).all()}
    assert remaining_titles == {"B"}


def test_delete_by_source_recipe_returns_zero_when_no_match(session):
    """Recipe id that doesn't exist in the list — clean 0, no error."""
    svc = HousewifeShoppingService(session)
    svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "x", "source_recipe_id": "rec_A"}],
    )
    assert svc.delete_by_source_recipe(
        tenant_id="t1", user_id="u1", recipe_id="rec_NONEXISTENT"
    ) == 0
    assert session.query(ShoppingListItem).count() == 1
