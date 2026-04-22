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


def test_coerce_category_empty_only_falls_to_default():
    """Pre-v1.2-launch the contract was 'unknown → другое'. New
    contract: only truly empty input falls to DEFAULT. Custom labels
    preserved — see test_coerce_category_preserves_custom_llm_category."""
    assert _coerce_category("") == DEFAULT_CATEGORY
    assert _coerce_category(None) == DEFAULT_CATEGORY
    assert _coerce_category("   ") == DEFAULT_CATEGORY


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


def test_add_items_preserves_custom_category(session):
    """New contract — custom LLM-supplied category names are
    preserved. Shopper sees a 'специи' section in Mini App even
    though it's not in the canonical taxonomy."""
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1",
        user_id="u1",
        items=[{"title": "шафран", "category": "специи"}],
    )
    assert rows[0].category == "специи"


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
# Cross-session visibility (Mini App <-> agent sync bug investigation)
# ---------------------------------------------------------------------------


def test_list_pending_sees_cross_session_mark_bought(session):
    """Agent's list_pending must see status changes made by the Mini App
    in a DIFFERENT SQLAlchemy session.

    Production bug (2026-04-22 tenant_tg_755682022): user yesterday
    added items via voice → today taps "bought" in Mini App (different
    session per request) → today asks agent "что в списке" → agent's
    session still returns the already-bought items, because its
    implicit read transaction holds an older snapshot.

    This test sets up two engines sharing one SQLite file so we can
    prove our service-level query re-reads fresh data on each call
    regardless of what OTHER sessions did after the first call."""
    import tempfile
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.db.models.core import Tenant, User

    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "cross.db"
        url = f"sqlite:///{db_file.as_posix()}"
        engine_a = create_engine(url)
        engine_b = create_engine(url)
        Base.metadata.create_all(engine_a)

        # Seed tenant + user in engine A.
        sess_a = sessionmaker(bind=engine_a)()
        sess_a.add(Tenant(id="t1", name="Test"))
        sess_a.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
        sess_a.commit()
        svc_a = HousewifeShoppingService(sess_a)

        # Two pending items via session A.
        rows = svc_a.add_items(
            tenant_id="t1", user_id="u1",
            items=[{"title": "молоко"}, {"title": "хлеб"}],
        )
        assert len(svc_a.list_pending(tenant_id="t1", user_id="u1")) == 2

        # Session B (separate engine, simulating Mini App request)
        # marks one as bought and commits.
        sess_b = sessionmaker(bind=engine_b)()
        svc_b = HousewifeShoppingService(sess_b)
        svc_b.mark_bought(tenant_id="t1", user_id="u1", ids=[rows[0].id])
        sess_b.close()

        # Session A queries again — must see only 1 pending. Without
        # session.expire_all()/commit() SQLAlchemy's identity map could
        # return the stale cached row.
        pending_after = svc_a.list_pending(tenant_id="t1", user_id="u1")
        assert len(pending_after) == 1, (
            "Agent's session failed to see Mini App's commit. "
            f"Expected 1 pending, got {len(pending_after)}: "
            f"{[r.title for r in pending_after]}"
        )
        assert pending_after[0].title == "хлеб"

        sess_a.close()
        engine_a.dispose()
        engine_b.dispose()


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


def test_shopping_categories_includes_лекарства():
    """'лекарства' is a first-class category (user requested 2026-04-22
    after LLM invented it on a medicine-containing list)."""
    assert "лекарства" in SHOPPING_CATEGORIES


def test_guess_category_medicine_names():
    from sreda.services.housewife_shopping import _guess_category
    for title in [
        "Беталок зок 100 мг", "Аевит", "парацетамол", "анальгин",
        "нурофен", "но-шпа", "Ибупрофен", "таблетки от головы",
        "витамин D", "капли для носа",
    ]:
        assert _guess_category(title) == "лекарства", (
            f"{title!r} should be лекарства, got {_guess_category(title)!r}"
        )


def test_coerce_category_preserves_custom_llm_category():
    """As of v1.2-launch: LLM/user can create any category, not just
    the 10 fixed buckets. `_coerce_category` normalises whitespace and
    case but doesn't force-map unknown into 'другое'."""
    assert _coerce_category("специи") == "специи"
    # Case + whitespace normalisation
    assert _coerce_category("ДЕТСКОЕ ПИТАНИЕ") == "детское питание"
    assert _coerce_category("  Канцелярия  ") == "канцелярия"


def test_coerce_category_known_category_still_matches():
    """Fixed taxonomy still resolves to canonical form (case-insensitive)."""
    assert _coerce_category("МОЛОЧНЫЕ") == "молочные"
    assert _coerce_category("молочные") == "молочные"


def test_coerce_category_empty_falls_back_to_другое():
    """Empty / None still falls back to DEFAULT_CATEGORY — without any
    category info we bucket as 'другое'."""
    assert _coerce_category(None) == "другое"
    assert _coerce_category("") == "другое"
    assert _coerce_category("   ") == "другое"


def test_coerce_category_truncates_long_strings():
    """Defensive cap — LLM hallucination could produce very long
    category names. Truncate to keep DB column reasonable and UI
    not-broken."""
    long = "очень длинное название категории " * 10  # >300 chars
    out = _coerce_category(long)
    assert 0 < len(out) <= 64


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


def test_update_item_changes_fields_in_place(session):
    """Avoid LLM's delete+add cycle: one update call, one commit."""
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[{"title": "Беталок зок 100 мг", "category": "другое"}],
    )
    item_id = rows[0].id
    updated = svc.update_item(
        tenant_id="t1", user_id="u1", item_id=item_id,
        category="лекарства",
    )
    assert updated is not None
    assert updated.category == "лекарства"
    # Unchanged fields stay
    assert updated.title == "Беталок зок 100 мг"
    assert updated.status == "pending"


def test_update_item_returns_none_for_unknown_id(session):
    svc = HousewifeShoppingService(session)
    assert svc.update_item(
        tenant_id="t1", user_id="u1", item_id="sh_nonexistent",
        category="лекарства",
    ) is None


def test_update_item_cross_tenant_returns_none(session):
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(tenant_id="t1", user_id="u1", items=[{"title": "X"}])
    # Different tenant can't modify
    assert svc.update_item(
        tenant_id="t2", user_id="u2", item_id=rows[0].id, category="лекарства",
    ) is None


def test_update_items_category_bulk_reassigns(session):
    """One tool call re-categorises many items — much cheaper than
    remove+add cycle through the LLM."""
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[
            {"title": "молоко"}, {"title": "хлеб"}, {"title": "сыр"},
        ],
    )
    n = svc.update_items_category(
        tenant_id="t1", user_id="u1",
        ids=[rows[0].id, rows[2].id],
        category="молочные",
    )
    assert n == 2
    after = {r.id: r.category for r in session.query(ShoppingListItem).all()}
    assert after[rows[0].id] == "молочные"
    assert after[rows[1].id] != "молочные"  # не трогали
    assert after[rows[2].id] == "молочные"


def test_add_items_skips_duplicate_titles_already_pending(session):
    """Hot bug 2026-04-22: shopping list gets duplicated when the
    user adds menu for Thursday then Friday separately — each
    generate_shopping_from_menu call re-added the same ingredients
    as new rows.

    Fix: add_items skips items whose normalised title already matches
    a pending row for (tenant, user). User sees one 'молоко' not
    two 'молоко / Молоко'."""
    svc = HousewifeShoppingService(session)
    # Day 1: add 3 items
    svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[
            {"title": "молоко", "quantity_text": "1 л"},
            {"title": "хлеб"},
            {"title": "картофель", "quantity_text": "1 кг"},
        ],
    )
    # Day 2: tries to add some of the same + new
    rows = svc.add_items(
        tenant_id="t1", user_id="u1",
        items=[
            {"title": "Молоко", "quantity_text": "500 мл"},     # dup (case)
            {"title": "морковь", "quantity_text": "2 шт"},      # new
            {"title": "  картофель  "},                           # dup (ws)
            {"title": "яблоко"},                                   # new
        ],
    )
    # Only 2 new rows added
    assert len(rows) == 2
    titles = {r.title for r in rows}
    assert titles == {"морковь", "яблоко"}
    # Total pending is 5 (3 original + 2 new)
    pending = svc.list_pending(tenant_id="t1", user_id="u1")
    assert len(pending) == 5


def test_add_items_does_NOT_dedup_against_bought_or_cancelled(session):
    """If user bought the item (status=bought) yesterday and adds it
    again today, it IS a new need — not a dup. Only pending collide."""
    svc = HousewifeShoppingService(session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1", items=[{"title": "молоко"}],
    )
    # Mark as bought
    svc.mark_bought(tenant_id="t1", user_id="u1", ids=[rows[0].id])
    # Add again — should succeed as NEW pending row
    new_rows = svc.add_items(
        tenant_id="t1", user_id="u1", items=[{"title": "молоко"}],
    )
    assert len(new_rows) == 1
    assert svc.list_pending(tenant_id="t1", user_id="u1")[0].title == "молоко"


def test_update_items_category_cross_tenant_safe(session):
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()
    svc = HousewifeShoppingService(session)
    r_t1 = svc.add_items(tenant_id="t1", user_id="u1", items=[{"title": "A"}])
    r_t2 = svc.add_items(tenant_id="t2", user_id="u2", items=[{"title": "B"}])
    # t2 user tries to reassign t1's row — no effect
    n = svc.update_items_category(
        tenant_id="t2", user_id="u2",
        ids=[r_t1[0].id, r_t2[0].id],
        category="лекарства",
    )
    assert n == 1  # only own row updated
    by_id = {r.id: r for r in session.query(ShoppingListItem).all()}
    assert by_id[r_t1[0].id].category != "лекарства"  # t1's untouched
    assert by_id[r_t2[0].id].category == "лекарства"


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
