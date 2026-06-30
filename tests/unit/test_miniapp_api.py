"""Unit tests for the Mini App API endpoints and auth dependency."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta
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


def _seed_housewife_sub(plan_key: str, active_until):
    """#200 Фаза 0: сидим housewife-план + активную подписку tenant_test на нём."""
    from datetime import UTC, datetime

    from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
    from sreda.db.session import get_session_factory

    session = get_session_factory()()
    try:
        plan = session.query(SubscriptionPlan).filter_by(plan_key=plan_key).first()
        if plan is None:
            plan = SubscriptionPlan(
                id=f"plan_{plan_key}",
                plan_key=plan_key,
                feature_key="housewife_assistant",
                title=f"HW {plan_key}",
                description="desc",
                price_rub=0,
                billing_period_days=30,
                is_public=True,
                is_active=True,
                sort_order=0,
            )
            session.add(plan)
            session.flush()
        session.add(
            TenantSubscription(
                id=f"sub_{plan_key}",
                tenant_id="tenant_test",
                plan_id=plan.id,
                feature_key="housewife_assistant",
                status="active",
                starts_at=datetime.now(UTC) - timedelta(days=5),
                active_until=active_until,
                cancel_at_period_end=False,
                quantity=1,
                next_cycle_quantity=1,
            )
        )
        session.commit()
    finally:
        session.close()


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
        # #103 (stale-test fix): the expiry window widened to 24h
        # (telegram_auth max_age_seconds=86400, industry standard). The old
        # 2h-ago (-7200) auth_date is now VALID → 200, so it no longer
        # exercised the 401 path. Use an age clearly beyond 24h (25h) so the
        # initData is genuinely expired.
        old_date = int(time.time()) - 90000  # 25h ago, > 24h max_age
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
        # Freshly provisioned — no active skills. #181: EDS Monitor is retired,
        # so it is NO LONGER surfaced in available_skills (tombstoned).
        assert data["active_skills"] == []
        assert not any(
            s["feature_key"] == "eds_monitor" for s in data["available_skills"]
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
        # #181: EDS Monitor retired — never surfaced in available_skills now.
        eds_plans = [s for s in data["available_skills"] if s["feature_key"] == "eds_monitor"]
        assert len(eds_plans) == 0


class TestMiniAppPhase0FeatureKeyResolution:
    """#200 Фаза 0: витрина резолвит housewife по feature_key, active_until=NULL=бессрочная,
    plan_key карточки = plan_key активной подписки (для subscribe/cancel)."""

    @pytest.mark.parametrize(
        "plan_key",
        ["sreda_free", "housewife_assistant_base", "housewife_grandfathered"],
    )
    def test_housewife_active_via_any_plan_with_null_active_until(self, seeded_client, plan_key):
        _seed_housewife_sub(plan_key, active_until=None)  # бессрочная (free/grandfathered)
        resp = seeded_client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {_make_init_data()}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        hw = [s for s in data["active_skills"] if s["feature_key"] == "housewife_assistant"]
        assert len(hw) == 1, f"housewife должен быть активным на плане {plan_key}"
        assert hw[0]["is_active"] is True
        # plan_key карточки = plan_key активной подписки (кнопки subscribe/cancel целятся в него)
        assert hw[0]["plan_key"] == plan_key
        # и НЕ дублируется в available
        assert not [
            s for s in data["available_skills"] if s["feature_key"] == "housewife_assistant"
        ]

    def test_post_merge_single_sub_on_sreda_free_builds_card(self, seeded_client):
        """После слияния единственная активная подписка на sreda_free → карточка строится,
        plan_key=sreda_free, даже без legacy-планов в БД (резолв по feature_key, не plan_key)."""
        _seed_housewife_sub("sreda_free", active_until=None)
        resp = seeded_client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {_make_init_data()}"},
        )
        data = resp.json()
        hw = [s for s in data["active_skills"] if s["feature_key"] == "housewife_assistant"]
        assert len(hw) == 1
        assert hw[0]["plan_key"] == "sreda_free"
        assert hw[0]["is_active"] is True

    def test_future_active_until_is_active_no_regression(self, seeded_client):
        """Анти-регресс платного пути: dated-подписка (active_until в будущем) активна."""
        from datetime import UTC, datetime

        _seed_housewife_sub("sreda_free", active_until=datetime.now(UTC) + timedelta(days=30))
        resp = seeded_client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {_make_init_data()}"},
        )
        hw = [s for s in resp.json()["active_skills"] if s["feature_key"] == "housewife_assistant"]
        assert len(hw) == 1 and hw[0]["is_active"] is True

    def test_expired_sub_falls_to_available_canonical(self, seeded_client):
        """Истёкшая подписка → не active; available-карточка из канонического плана (plan_key)."""
        from datetime import UTC, datetime

        _seed_housewife_sub("sreda_free", active_until=datetime.now(UTC) - timedelta(days=1))
        resp = seeded_client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {_make_init_data()}"},
        )
        data = resp.json()
        assert not [s for s in data["active_skills"] if s["feature_key"] == "housewife_assistant"]
        avail = [s for s in data["available_skills"] if s["feature_key"] == "housewife_assistant"]
        assert len(avail) == 1 and avail[0]["plan_key"] == "sreda_free"


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
        # #181: EDS plans are tombstoned out of the public catalog.
        plan_keys = [p["plan_key"] for p in data["plans"]]
        assert "eds_monitor_base" not in plan_keys
        assert "eds_monitor_extra_account" not in plan_keys


class TestMiniAppSubscribe:
    def test_subscribe_eds_base(self, seeded_client):
        # #181 Phase B: subscribing to a retired skill is a tombstone. The
        # endpoint still answers 200 (old links don't 404) but reports ok=False
        # with the disabled message and creates NO active subscription.
        init_data = _make_init_data()
        headers = {"Authorization": f"tma {init_data}"}

        # Subscribe attempt → disabled tombstone
        resp = seeded_client.post(
            "/miniapp/api/v1/subscribe",
            json={"plan_key": "eds_monitor_base"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["message"] == "Это умение больше не поддерживается."

        # Summary must NOT show eds_monitor as active.
        resp = seeded_client.get("/miniapp/api/v1/summary", headers=headers)
        data = resp.json()
        active_keys = [s["feature_key"] for s in data["active_skills"]]
        assert "eds_monitor" not in active_keys

    def test_subscribe_unknown_plan_returns_400(self, seeded_client):
        init_data = _make_init_data()
        resp = seeded_client.post(
            "/miniapp/api/v1/subscribe",
            json={"plan_key": "nonexistent_plan"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 400

    def test_cancel_eds_base(self, seeded_client):
        # #181 Phase B: cancel on a retired EDS plan_key is a disabled tombstone
        # (200, ok=False), not a 404/500.
        init_data = _make_init_data()
        headers = {"Authorization": f"tma {init_data}"}

        resp = seeded_client.post(
            "/miniapp/api/v1/cancel",
            json={"plan_key": "eds_monitor_base"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["message"] == "Это умение больше не поддерживается."

    def test_subscribe_voice_transcription(self, seeded_client):
        # #204 Фаза 2: voice_transcription_base — tombstone (is_active=False на
        # проде, миграция 0018). Подписка через Mini App на него ДОЛЖНА быть
        # отклонена 400 deprecated_plan — реактивация закрыта и на endpoint.
        init_data = _make_init_data()
        headers = {"Authorization": f"tma {init_data}"}

        # Seed the voice plan in its production tombstone state (is_active=False).
        from sreda.db.session import get_session_factory
        from sreda.db.models.billing import SubscriptionPlan, TenantSubscription

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
                        is_active=False,
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
        assert resp.status_code == 400
        assert resp.json()["detail"] == "deprecated_plan"

        # No active voice subscription was created by the denied request.
        session = get_session_factory()()
        try:
            active = (
                session.query(TenantSubscription)
                .filter(
                    TenantSubscription.feature_key == "voice_transcription",
                    TenantSubscription.status == "active",
                )
                .count()
            )
            assert active == 0
        finally:
            session.close()


class TestMiniAppFamilyPatch:
    """PATCH /api/v1/family/{member_id} — Mini App member editor."""

    def _seed_member(self):
        from sreda.db.session import get_session_factory
        from sreda.services.housewife_family import HousewifeFamilyService

        session = get_session_factory()()
        try:
            m = HousewifeFamilyService(session).add_member(
                tenant_id="tenant_test", user_id="user_test",
                name="Катя", role="spouse", birth_year=1988,
                notes="аллергия на горчицу",
            )
            return m.id
        finally:
            session.close()

    def test_patch_updates_single_field(self, seeded_client):
        member_id = self._seed_member()
        init_data = _make_init_data()
        resp = seeded_client.patch(
            f"/miniapp/api/v1/family/{member_id}",
            json={"birth_year": 1989},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["member"]["birth_year"] == 1989
        # Other fields should be unchanged
        assert body["member"]["name"] == "Катя"
        assert body["member"]["notes"] == "аллергия на горчицу"

    def test_patch_updates_multiple_fields(self, seeded_client):
        member_id = self._seed_member()
        init_data = _make_init_data()
        resp = seeded_client.patch(
            f"/miniapp/api/v1/family/{member_id}",
            json={
                "name": "Екатерина",
                "role": "spouse",
                "notes": "аллергия на горчицу + непереносимость лактозы",
            },
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        m = resp.json()["member"]
        assert m["name"] == "Екатерина"
        assert m["notes"] == "аллергия на горчицу + непереносимость лактозы"
        # Unspecified field (birth_year) stays.
        assert m["birth_year"] == 1988

    def test_patch_unknown_id_returns_404(self, seeded_client):
        init_data = _make_init_data()
        resp = seeded_client.patch(
            "/miniapp/api/v1/family/fm_nonexistent",
            json={"name": "X"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 404

    def test_patch_invalid_role_returns_400(self, seeded_client):
        member_id = self._seed_member()
        init_data = _make_init_data()
        resp = seeded_client.patch(
            f"/miniapp/api/v1/family/{member_id}",
            json={"role": "alien"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 400


class TestMiniAppClearAllShopping:
    """POST /api/v1/shopping/clear-all — Mini App "очистить всё" button."""

    def _seed_items(self):
        from sreda.db.session import get_session_factory
        from sreda.services.housewife_shopping import HousewifeShoppingService

        session = get_session_factory()()
        try:
            svc = HousewifeShoppingService(session)
            rows = svc.add_items(
                tenant_id="tenant_test", user_id="user_test",
                items=[{"title": "A"}, {"title": "B"}, {"title": "C"}],
            )
            # Mark one as bought so clear-all doesn't touch it.
            svc.mark_bought(
                tenant_id="tenant_test", user_id="user_test",
                ids=[rows[1].id],
            )
            return [rows[0].id, rows[1].id, rows[2].id]
        finally:
            session.close()

    def test_clear_all_cancels_pending_only(self, seeded_client):
        ids = self._seed_items()
        init_data = _make_init_data()
        resp = seeded_client.post(
            "/miniapp/api/v1/shopping/clear-all",
            json={},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cleared": 2}

        # Verify bought item survives.
        from sreda.db.models.housewife_food import ShoppingListItem
        from sreda.db.session import get_session_factory

        session = get_session_factory()()
        try:
            statuses = {
                r.id: r.status
                for r in session.query(ShoppingListItem).filter(
                    ShoppingListItem.id.in_(ids)
                ).all()
            }
            assert statuses[ids[0]] == "cancelled"
            assert statuses[ids[1]] == "bought"  # untouched
            assert statuses[ids[2]] == "cancelled"
        finally:
            session.close()

    def test_clear_all_on_empty_list_returns_zero(self, seeded_client):
        init_data = _make_init_data()
        resp = seeded_client.post(
            "/miniapp/api/v1/shopping/clear-all",
            json={},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cleared": 0}


class TestMiniAppScheduleWeek:
    """GET /api/v1/schedule/week — Mini App «Расписание» недельный вид.

    Replaces the 2026-04-23 single-day endpoint. The week view is what
    actually maps to the user's mental model: recurring tasks should
    visibly span every day they fire on, and the current week is the
    natural scope for «что у меня сегодня и дальше».

    Shape:
      ``{"week_start": "YYYY-MM-DD",
         "inbox": [task_dict, ...],    # current week only, else []
         "days": [
           {"date": "...", "label": "Понедельник, 20 апреля",
            "is_past": bool, "tasks": [...]},
           ...  # exactly 7 entries
         ]}``
    """

    @staticmethod
    def _current_monday_utc():
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        today = _dt.now(_tz.utc).date()
        return today - timedelta(days=today.weekday())

    def test_week_endpoint_default_returns_current_week(self, seeded_client):
        """Empty DB, no start param — 200 with 7 days, ISO labels,
        is_past correct relative to today."""
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        init_data = _make_init_data()
        resp = seeded_client.get(
            "/miniapp/api/v1/schedule/week",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["week_start"] == self._current_monday_utc().isoformat()
        assert body["inbox"] == []
        assert len(body["days"]) == 7

        today = _dt.now(_tz.utc).date()
        for day in body["days"]:
            assert "date" in day
            assert "label" in day and day["label"]
            assert "is_past" in day
            assert "tasks" in day
            day_date = _dt.fromisoformat(day["date"]).date()
            assert day["is_past"] == (day_date < today)

    def test_week_endpoint_with_start_param_returns_that_week(self, seeded_client):
        """Explicit future start → that Monday, inbox omitted."""
        from datetime import datetime as _dt

        future_monday = self._current_monday_utc() + timedelta(days=7)
        init_data = _make_init_data()
        resp = seeded_client.get(
            f"/miniapp/api/v1/schedule/week?start={future_monday.isoformat()}",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["week_start"] == future_monday.isoformat()
        assert body["inbox"] == []
        # All days are future → no is_past=true.
        assert all(d["is_past"] is False for d in body["days"])
        # First day's date matches the requested Monday.
        assert _dt.fromisoformat(body["days"][0]["date"]).date() == future_monday

    def test_week_endpoint_inbox_in_current_week_only(self, seeded_client):
        """Undated tasks surface in inbox only when start=current Monday.
        Requesting next week must return inbox=[] to avoid duplication."""
        from sreda.db.session import get_session_factory
        from sreda.services.tasks import TaskService

        session = get_session_factory()()
        try:
            TaskService(session).add(
                tenant_id="tenant_test", user_id="user_test",
                title="Undated item",  # no scheduled_date → inbox
            )
        finally:
            session.close()

        init_data = _make_init_data()
        resp_current = seeded_client.get(
            "/miniapp/api/v1/schedule/week",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp_current.status_code == 200
        inbox = resp_current.json()["inbox"]
        assert len(inbox) == 1 and inbox[0]["title"] == "Undated item"

        next_monday = self._current_monday_utc() + timedelta(days=7)
        resp_next = seeded_client.get(
            f"/miniapp/api/v1/schedule/week?start={next_monday.isoformat()}",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp_next.json()["inbox"] == []

    def test_week_endpoint_recurring_task_spans_all_days(self, seeded_client):
        """A daily-recurring task must appear in every day of the week."""
        from datetime import time as _time

        from sreda.db.session import get_session_factory
        from sreda.services.tasks import TaskService

        start_monday = self._current_monday_utc()
        session = get_session_factory()()
        try:
            TaskService(session).add(
                tenant_id="tenant_test", user_id="user_test",
                title="Прогулка",
                scheduled_date=start_monday,
                time_start=_time(18, 0),
                recurrence_rule="FREQ=DAILY;BYHOUR=15;BYMINUTE=0",
            )
        finally:
            session.close()

        init_data = _make_init_data()
        resp = seeded_client.get(
            "/miniapp/api/v1/schedule/week",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        for day in resp.json()["days"]:
            titles = [t["title"] for t in day["tasks"]]
            assert "Прогулка" in titles, f"missing on {day['date']}"


class TestMiniAppWeeklyMenu:
    """#235 — read-only weekly-menu API restore (GET grid for #/menu screen)."""

    def test_menu_item_dict_serialises_own_recipe_title_and_calories(self):
        from types import SimpleNamespace

        from sreda.api.routes.miniapp import _menu_item_dict

        recipe = SimpleNamespace(
            title="Борщ", calories_per_serving=250, tenant_id="t", user_id="u",
        )
        item = SimpleNamespace(
            id="i1", day_of_week=2, meal_type="dinner",
            recipe_id="r1", free_text=None, notes=None, recipe=recipe,
        )
        out = _menu_item_dict(item, tenant_id="t", user_id="u")
        assert out["recipe_title"] == "Борщ"
        assert out["recipe_calories"] == 250
        assert out["day_of_week"] == 2 and out["meal_type"] == "dinner"

    def test_menu_item_dict_free_text_cell_has_no_recipe_fields(self):
        from types import SimpleNamespace

        from sreda.api.routes.miniapp import _menu_item_dict

        item = SimpleNamespace(
            id="i2", day_of_week=0, meal_type="breakfast",
            recipe_id=None, free_text="Овсянка", notes=None, recipe=None,
        )
        out = _menu_item_dict(item, tenant_id="t", user_id="u")
        assert out["free_text"] == "Овсянка"
        assert out["recipe_title"] is None
        assert out["recipe_calories"] is None

    def test_menu_item_dict_hides_cross_tenant_recipe(self):
        """R1 MAJOR: a recipe_id pointing at ANOTHER (tenant,user)'s recipe
        must NOT leak its title/calories through the read endpoint."""
        from types import SimpleNamespace

        from sreda.api.routes.miniapp import _menu_item_dict

        foreign = SimpleNamespace(
            title="Чужой борщ", calories_per_serving=300,
            tenant_id="other_t", user_id="other_u",
        )
        item = SimpleNamespace(
            id="i3", day_of_week=1, meal_type="lunch",
            recipe_id="r9", free_text=None, notes=None, recipe=foreign,
        )
        out = _menu_item_dict(item, tenant_id="tenant_test", user_id="user_test")
        assert out["recipe_title"] is None     # foreign recipe NOT leaked
        assert out["recipe_calories"] is None
        assert out["recipe_id"] == "r9"        # own id passes through

    def test_weekly_menu_empty_returns_plan_none(self, seeded_client):
        resp = seeded_client.get(
            "/miniapp/api/v1/weekly-menu",
            headers={"Authorization": f"tma {_make_init_data()}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"plan": None}

    def test_weekly_menu_returns_grid_after_seeding(self, seeded_client):
        from datetime import date

        from sreda.db.session import get_session_factory
        from sreda.services.housewife_menu import HousewifeMenuService, MenuCellInput

        session = get_session_factory()()
        try:
            HousewifeMenuService(session).plan_week(
                tenant_id="tenant_test", user_id="user_test",
                week_start=date(2026, 6, 22),
                cells=[
                    MenuCellInput(day_of_week=0, meal_type="breakfast", free_text="Овсянка"),
                    MenuCellInput(day_of_week=2, meal_type="dinner", free_text="Борщ"),
                ],
            )
        finally:
            session.close()

        resp = seeded_client.get(
            "/miniapp/api/v1/weekly-menu",
            headers={"Authorization": f"tma {_make_init_data()}"},
        )
        assert resp.status_code == 200
        plan = resp.json()["plan"]
        assert plan is not None
        cells = {(c["day_of_week"], c["meal_type"]): c for c in plan["items"]}
        assert cells[(0, "breakfast")]["free_text"] == "Овсянка"
        assert cells[(2, "dinner")]["free_text"] == "Борщ"
        assert "recipe_title" in cells[(0, "breakfast")]  # serializer shape intact

    def test_weekly_menu_invalid_week_start_returns_400(self, seeded_client):
        """R1: bad ?week_start= → controlled 400, not 500 (mirror schedule/week)."""
        resp = seeded_client.get(
            "/miniapp/api/v1/weekly-menu?week_start=notadate",
            headers={"Authorization": f"tma {_make_init_data()}"},
        )
        assert resp.status_code == 400


