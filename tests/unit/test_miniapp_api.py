"""Unit tests for the Mini App API endpoints and auth dependency."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from sreda.main import create_app

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def _make_init_data(
    *,
    bot_token: str = BOT_TOKEN,
    user_id: int = 352612382,
    first_name: str = "Test",
    username: str = "testuser",
    auth_date: int | None = None,
) -> str:
    if auth_date is None:
        auth_date = int(time.time())
    user_json = json.dumps(
        {"id": user_id, "first_name": first_name, "username": username},
        separators=(",", ":"),
    )
    params: dict[str, str] = {"auth_date": str(auth_date), "user": user_json}
    sorted_pairs = sorted(params.items(), key=lambda p: p[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_pairs)
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    params["hash"] = computed_hash
    return urlencode(params)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """Create a TestClient with in-memory SQLite and a known bot token."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.test.local")

    from sreda.config.settings import get_settings
    from sreda.api.deps import reset_rate_limiters
    from sreda.db.session import get_engine, get_session_factory
    from sreda.db.base import Base

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    reset_rate_limiters()

    Base.metadata.create_all(get_engine())

    with TestClient(create_app()) as c:
        yield c

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    reset_rate_limiters()


@pytest.fixture()
def seeded_client(client, monkeypatch, tmp_path):
    """Client with a pre-seeded user and tenant."""
    from sreda.db.session import get_session_factory
    from sreda.db.repositories.seed import SeedRepository

    session = get_session_factory()()
    try:
        SeedRepository(session).ensure_tenant_bundle(
            tenant_id="tenant_test",
            tenant_name="Test User",
            workspace_id="ws_test",
            workspace_name="Test",
            user_id="user_test",
            telegram_account_id="352612382",
            assistant_id="assistant_test",
            assistant_name="Среда",
            eds_monitor_enabled=False,
        )
        session.commit()
    finally:
        session.close()
    return client


class TestMiniAppHTML:
    def test_get_miniapp_page_returns_html(self, client):
        resp = client.get("/miniapp/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "telegram-web-app.js" in resp.text
        assert "Мои подписки" in resp.text or "subscriptions" in resp.text.lower()


class TestMiniAppAuth:
    def test_missing_auth_header_returns_401(self, client):
        resp = client.get("/miniapp/api/v1/summary")
        assert resp.status_code == 401

    def test_invalid_init_data_returns_401(self, client):
        resp = client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": "tma invalid_data"},
        )
        assert resp.status_code == 401

    def test_expired_init_data_returns_401(self, seeded_client):
        old_date = int(time.time()) - 7200
        init_data = _make_init_data(auth_date=old_date)
        resp = seeded_client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 401

    def test_unknown_user_auto_provisioned(self, client):
        # Valid signature but user not in DB — Mini App must be usable
        # immediately, so the auth layer lazily provisions a tenant
        # bundle instead of 401. See _require_miniapp_auth.
        init_data = _make_init_data(user_id=999999)
        resp = client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Freshly provisioned — no active skills, EDS Monitor in available.
        assert data["active_skills"] == []
        assert any(
            s["plan_key"] == "eds_monitor_base" for s in data["available_skills"]
        )


class TestMiniAppSummary:
    def test_summary_returns_valid_json(self, seeded_client):
        init_data = _make_init_data()
        resp = seeded_client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "active_skills" in data
        assert "available_skills" in data
        assert "eds_subscriptions" in data
        assert isinstance(data["active_skills"], list)
        assert isinstance(data["available_skills"], list)

    def test_new_user_has_no_active_skills(self, seeded_client):
        init_data = _make_init_data()
        resp = seeded_client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {init_data}"},
        )
        data = resp.json()
        assert len(data["active_skills"]) == 0
        # EDS Monitor should be available
        eds_plans = [s for s in data["available_skills"] if s["feature_key"] == "eds_monitor"]
        assert len(eds_plans) == 1


class TestMiniAppPlans:
    def test_plans_returns_list(self, seeded_client):
        init_data = _make_init_data()
        resp = seeded_client.get(
            "/miniapp/api/v1/plans",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "plans" in data
        assert isinstance(data["plans"], list)
        # At least EDS base and extra plans exist
        plan_keys = [p["plan_key"] for p in data["plans"]]
        assert "eds_monitor_base" in plan_keys


class TestMiniAppSubscribe:
    def test_subscribe_eds_base(self, seeded_client):
        init_data = _make_init_data()
        headers = {"Authorization": f"tma {init_data}"}

        # Subscribe
        resp = seeded_client.post(
            "/miniapp/api/v1/subscribe",
            json={"plan_key": "eds_monitor_base"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

        # Verify active in summary
        resp = seeded_client.get("/miniapp/api/v1/summary", headers=headers)
        data = resp.json()
        active_keys = [s["feature_key"] for s in data["active_skills"]]
        assert "eds_monitor" in active_keys

    def test_subscribe_unknown_plan_returns_400(self, seeded_client):
        init_data = _make_init_data()
        resp = seeded_client.post(
            "/miniapp/api/v1/subscribe",
            json={"plan_key": "nonexistent_plan"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 400

    def test_cancel_eds_base(self, seeded_client):
        init_data = _make_init_data()
        headers = {"Authorization": f"tma {init_data}"}

        # Subscribe first
        seeded_client.post(
            "/miniapp/api/v1/subscribe",
            json={"plan_key": "eds_monitor_base"},
            headers=headers,
        )

        # Cancel
        resp = seeded_client.post(
            "/miniapp/api/v1/cancel",
            json={"plan_key": "eds_monitor_base"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_subscribe_voice_transcription(self, seeded_client):
        init_data = _make_init_data()
        headers = {"Authorization": f"tma {init_data}"}

        # Need voice plan seeded first
        from sreda.db.session import get_session_factory
        from sreda.db.models.billing import SubscriptionPlan

        session = get_session_factory()()
        try:
            existing = (
                session.query(SubscriptionPlan)
                .filter(SubscriptionPlan.plan_key == "voice_transcription_base")
                .one_or_none()
            )
            if existing is None:
                session.add(
                    SubscriptionPlan(
                        id="plan_voice",
                        plan_key="voice_transcription_base",
                        feature_key="voice_transcription",
                        title="Распознавание голоса",
                        description="Транскрибация голосовых сообщений",
                        price_rub=0,
                        billing_period_days=30,
                        is_public=True,
                        is_active=True,
                        sort_order=30,
                    )
                )
                session.commit()
        finally:
            session.close()

        resp = seeded_client.post(
            "/miniapp/api/v1/subscribe",
            json={"plan_key": "voice_transcription_base"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestMiniAppMenuItemPatch:
    """PATCH /api/v1/weekly-menu/{plan_id}/item — Stage 5 inline editor."""

    def _seed_plan(self):
        """Create an empty menu plan directly via service, return plan_id."""
        from sreda.db.session import get_session_factory
        from sreda.services.housewife_menu import HousewifeMenuService

        session = get_session_factory()()
        try:
            plan = HousewifeMenuService(session).plan_week(
                tenant_id="tenant_test",
                user_id="user_test",
                week_start="2026-04-20",
                cells=[],  # empty plan — all cells created via PATCH
            )
            return plan.id
        finally:
            session.close()

    def test_patch_creates_cell_with_free_text(self, seeded_client):
        plan_id = self._seed_plan()
        init_data = _make_init_data()
        resp = seeded_client.patch(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/item",
            json={
                "day_of_week": 2,
                "meal_type": "dinner",
                "free_text": "паста карбонара",
                "notes": "на двоих",
            },
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["item"]["free_text"] == "паста карбонара"
        assert data["item"]["day_of_week"] == 2
        assert data["item"]["meal_type"] == "dinner"

    def test_patch_clears_cell_when_both_empty(self, seeded_client):
        """Passing recipe_id=None + free_text empty/None deletes the cell.
        Service returns None → endpoint reports cleared=True (not 404)."""
        plan_id = self._seed_plan()
        init_data = _make_init_data()

        # First create a cell
        seeded_client.patch(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/item",
            json={"day_of_week": 0, "meal_type": "breakfast", "free_text": "каша"},
            headers={"Authorization": f"tma {init_data}"},
        )
        # Then clear it
        resp = seeded_client.patch(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/item",
            json={"day_of_week": 0, "meal_type": "breakfast"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cleared": True}

    def test_patch_unknown_plan_returns_404(self, seeded_client):
        init_data = _make_init_data()
        resp = seeded_client.patch(
            "/miniapp/api/v1/weekly-menu/nonexistent_plan/item",
            json={"day_of_week": 0, "meal_type": "lunch", "free_text": "x"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 404

    def test_patch_invalid_meal_type_returns_400(self, seeded_client):
        plan_id = self._seed_plan()
        init_data = _make_init_data()
        resp = seeded_client.patch(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/item",
            json={"day_of_week": 0, "meal_type": "teatime", "free_text": "чай"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 400


class TestMiniAppDayShopping:
    """POST /api/v1/weekly-menu/{plan_id}/generate-shopping-for-day"""

    def _seed_plan_with_recipes(self):
        from sreda.db.session import get_session_factory
        from sreda.services.housewife_menu import HousewifeMenuService
        from sreda.services.housewife_recipes import HousewifeRecipeService

        session = get_session_factory()()
        try:
            recipe_svc = HousewifeRecipeService(session)
            r_day0, _ = recipe_svc.save_recipe(
                tenant_id="tenant_test", user_id="user_test",
                title="Каша", ingredients=[{"title": "овсянка"}],
                servings=2, source="user_dictated",
            )
            r_day1, _ = recipe_svc.save_recipe(
                tenant_id="tenant_test", user_id="user_test",
                title="Суп", ingredients=[{"title": "вода"}, {"title": "курица"}],
                servings=2, source="user_dictated",
            )
            plan = HousewifeMenuService(session).plan_week(
                tenant_id="tenant_test", user_id="user_test",
                week_start="2026-04-20",
                cells=[
                    {"day_of_week": 0, "meal_type": "breakfast", "recipe_id": r_day0.id},
                    {"day_of_week": 1, "meal_type": "lunch", "recipe_id": r_day1.id},
                ],
            )
            return plan.id
        finally:
            session.close()

    def test_generate_day_adds_only_that_day(self, seeded_client):
        plan_id = self._seed_plan_with_recipes()
        init_data = _make_init_data()
        resp = seeded_client.post(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/generate-shopping-for-day",
            json={"day_of_week": 1},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        # day 1 had "Суп" with 2 ingredients
        assert resp.json() == {"ok": True, "added": 2}

    def test_generate_day_returns_zero_for_empty_day(self, seeded_client):
        plan_id = self._seed_plan_with_recipes()
        init_data = _make_init_data()
        resp = seeded_client.post(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/generate-shopping-for-day",
            json={"day_of_week": 5},  # no cells that day
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "added": 0}

    def test_generate_day_invalid_day_returns_400(self, seeded_client):
        plan_id = self._seed_plan_with_recipes()
        init_data = _make_init_data()
        resp = seeded_client.post(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/generate-shopping-for-day",
            json={"day_of_week": 9},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 400


class TestMiniAppRegenItem:
    """POST /api/v1/weekly-menu/{plan_id}/regenerate-item

    The endpoint calls _suggest_menu_cell_via_llm internally; we
    monkeypatch that to control what "the LLM" returns, avoiding an
    actual model dependency in unit tests. Three scenarios covered:
    successful swap with shopping sync, recipe-still-used (shopping
    preserved), and the concurrent-request 409 guard.
    """

    def _seed_plan_two_cells_same_recipe(self):
        """Two cells on different days sharing the same recipe — used to
        verify that replacing ONE cell doesn't wipe shopping for the
        recipe still used by the OTHER cell."""
        from sreda.db.session import get_session_factory
        from sreda.services.housewife_menu import HousewifeMenuService
        from sreda.services.housewife_recipes import HousewifeRecipeService
        from sreda.services.housewife_shopping import HousewifeShoppingService

        session = get_session_factory()()
        try:
            r, _ = HousewifeRecipeService(session).save_recipe(
                tenant_id="tenant_test", user_id="user_test",
                title="Плов", servings=2,
                ingredients=[{"title": "рис", "quantity_text": "300 г"}],
                source="user_dictated",
            )
            plan = HousewifeMenuService(session).plan_week(
                tenant_id="tenant_test", user_id="user_test",
                week_start="2026-04-20",
                cells=[
                    {"day_of_week": 1, "meal_type": "lunch", "recipe_id": r.id},
                    {"day_of_week": 3, "meal_type": "dinner", "recipe_id": r.id},
                ],
            )
            # Manually add a shopping item tied to the shared recipe.
            HousewifeShoppingService(session).add_items(
                tenant_id="tenant_test", user_id="user_test",
                items=[{
                    "title": "рис",
                    "quantity_text": "600 г",
                    "source_recipe_id": r.id,
                }],
            )
            return plan.id, r.id
        finally:
            session.close()

    def test_regen_swaps_cell_and_returns_new_item(
        self, seeded_client, monkeypatch
    ):
        """Happy path — LLM suggests new free_text, cell updates, 200."""
        plan_id, _ = self._seed_plan_two_cells_same_recipe()

        def fake_suggest(**kwargs):
            return {"recipe_id": None, "free_text": "Окрошка", "notes": None}

        monkeypatch.setattr(
            "sreda.api.routes.miniapp._suggest_menu_cell_via_llm",
            fake_suggest,
        )

        init_data = _make_init_data()
        resp = seeded_client.post(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/regenerate-item",
            json={"day_of_week": 1, "meal_type": "lunch"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["item"]["free_text"] == "Окрошка"
        # The shared recipe is still used on day 3 → shopping preserved.
        assert body["removed_from_shopping"] == 0

    def test_regen_preserves_shopping_when_recipe_still_used(
        self, seeded_client, monkeypatch
    ):
        """After swapping day=1 to a new dish, the rice item must stay —
        day=3 still uses the original recipe."""
        plan_id, shared_recipe_id = self._seed_plan_two_cells_same_recipe()

        monkeypatch.setattr(
            "sreda.api.routes.miniapp._suggest_menu_cell_via_llm",
            lambda **kw: {"recipe_id": None, "free_text": "Окрошка", "notes": None},
        )

        init_data = _make_init_data()
        seeded_client.post(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/regenerate-item",
            json={"day_of_week": 1, "meal_type": "lunch"},
            headers={"Authorization": f"tma {init_data}"},
        )

        # Verify: shopping item for the shared recipe is still there.
        from sreda.db.models.housewife_food import ShoppingListItem
        from sreda.db.session import get_session_factory

        session = get_session_factory()()
        try:
            items = session.query(ShoppingListItem).filter_by(
                source_recipe_id=shared_recipe_id
            ).all()
            assert len(items) == 1, (
                "Shopping for rice should NOT be deleted when another "
                "cell still references the recipe"
            )
        finally:
            session.close()

    def test_regen_removes_shopping_when_recipe_orphaned(
        self, seeded_client, monkeypatch
    ):
        """After swapping the ONLY cell that uses a recipe, its shopping
        items should be cleaned up."""
        from sreda.db.session import get_session_factory
        from sreda.services.housewife_menu import HousewifeMenuService
        from sreda.services.housewife_recipes import HousewifeRecipeService
        from sreda.services.housewife_shopping import HousewifeShoppingService

        session = get_session_factory()()
        try:
            r, _ = HousewifeRecipeService(session).save_recipe(
                tenant_id="tenant_test", user_id="user_test",
                title="Блины", servings=2,
                ingredients=[{"title": "мука"}],
                source="user_dictated",
            )
            plan = HousewifeMenuService(session).plan_week(
                tenant_id="tenant_test", user_id="user_test",
                week_start="2026-04-20",
                cells=[
                    {"day_of_week": 0, "meal_type": "breakfast", "recipe_id": r.id},
                ],
            )
            HousewifeShoppingService(session).add_items(
                tenant_id="tenant_test", user_id="user_test",
                items=[{"title": "мука", "source_recipe_id": r.id}],
            )
            plan_id = plan.id
            orphan_recipe_id = r.id
        finally:
            session.close()

        monkeypatch.setattr(
            "sreda.api.routes.miniapp._suggest_menu_cell_via_llm",
            lambda **kw: {"recipe_id": None, "free_text": "Овсянка", "notes": None},
        )
        init_data = _make_init_data()
        resp = seeded_client.post(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/regenerate-item",
            json={"day_of_week": 0, "meal_type": "breakfast"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        assert resp.json()["removed_from_shopping"] == 1

        # Verify DB too.
        from sreda.db.models.housewife_food import ShoppingListItem

        session = get_session_factory()()
        try:
            assert session.query(ShoppingListItem).filter_by(
                source_recipe_id=orphan_recipe_id
            ).count() == 0
        finally:
            session.close()

    def test_regen_rejects_concurrent_same_cell_with_409(
        self, seeded_client, monkeypatch
    ):
        """Two parallel regen calls on the SAME (plan, day, meal_type)
        cell: the second one short-circuits with 409 instead of racing
        through update_item. Uses threading to drive true concurrency
        against the in-memory lock."""
        import threading

        plan_id, _ = self._seed_plan_two_cells_same_recipe()

        # Block the LLM call so we can run a second request while the
        # first is still "inside" the critical section.
        release = threading.Event()
        started = threading.Event()

        def slow_suggest(**kwargs):
            started.set()
            release.wait(timeout=5)
            return {"recipe_id": None, "free_text": "A", "notes": None}

        monkeypatch.setattr(
            "sreda.api.routes.miniapp._suggest_menu_cell_via_llm",
            slow_suggest,
        )

        init_data = _make_init_data()
        headers = {"Authorization": f"tma {init_data}"}
        payload = {"day_of_week": 1, "meal_type": "lunch"}

        first_result: dict = {}

        def first_request():
            r = seeded_client.post(
                f"/miniapp/api/v1/weekly-menu/{plan_id}/regenerate-item",
                json=payload, headers=headers,
            )
            first_result["resp"] = r

        t = threading.Thread(target=first_request)
        t.start()
        # Wait until the first request is inside the slow LLM call —
        # at that point the inflight set contains our cell key.
        assert started.wait(timeout=5), "first request never reached slow_suggest"

        # Second call on the same cell — should get rejected fast.
        second = seeded_client.post(
            f"/miniapp/api/v1/weekly-menu/{plan_id}/regenerate-item",
            json=payload, headers=headers,
        )
        assert second.status_code == 409
        assert second.json()["detail"] == "regen_already_in_flight"

        # Let the first finish.
        release.set()
        t.join(timeout=10)
        assert first_result["resp"].status_code == 200
