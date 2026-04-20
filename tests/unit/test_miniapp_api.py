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
