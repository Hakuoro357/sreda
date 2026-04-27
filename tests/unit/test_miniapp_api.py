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


