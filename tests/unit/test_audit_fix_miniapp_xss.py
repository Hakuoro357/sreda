"""Регрессионные тесты фиксов аудита 2026-07-18 (воркер: miniapp-xss).

Покрытие:
- api-admin #1 MAJOR — stored-XSS раковины в miniapp subscriptions.html:
  атрибутные/JS-строковые контексты обязаны идти через escAttr (кавычки),
  recipe source_url — через safeHttpUrl scheme-фильтр (javascript:).
- api-admin #2 MINOR — client_diagnostic не логирует сырое тело запроса.
- api-admin #6 MINOR — TG lazy-provision race: IntegrityError-recovery
  (паритет с MAX-веткой, Codex R3).
- api-admin #10 MINOR — /subscribe generic is_feature_disabled guard.
- cross-security N3 MINOR — security-headers на /miniapp ответах.

Без сети и без PG: SQLite в tmp_path по образцу test_miniapp_api.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "sreda" / "miniapp" / "templates" / "subscriptions.html"
)


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
    """TestClient с SQLite в tmp_path и известным bot-token (паттерн test_miniapp_api)."""
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

    # sreda_free тариф — прод-предпосылка (0041); lazy-provision без него бросает.
    from sreda.db.models.billing import SubscriptionPlan
    _seed_sess = get_session_factory()()
    try:
        if _seed_sess.query(SubscriptionPlan).filter_by(plan_key="sreda_free").first() is None:
            _seed_sess.add(SubscriptionPlan(
                id="plan_free", plan_key="sreda_free",
                feature_key="housewife_assistant", title="Free", description="",
                price_rub=0,
            ))
            _seed_sess.commit()
    finally:
        _seed_sess.close()

    from sreda.main import create_app
    with TestClient(create_app()) as c:
        yield c

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    reset_rate_limiters()


@pytest.fixture()
def seeded_client(client):
    """Client с пред-seeded юзером/тенантом (как в test_miniapp_api)."""
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


# ---------------------------------------------------------------------------
# api-admin #1 MAJOR: static guards против stored-XSS раковин в шаблоне
# ---------------------------------------------------------------------------


class TestTemplateXssSinks:
    """Статические гарды: escHtml не должен появляться в атрибутных и
    JS-строковых контекстах subscriptions.html (он не экранирует `"`)."""

    # + escHtml(x) + '\... — вставка в JS-строку внутри onclick="..."
    _RX_JS_ARG = re.compile(
        r"\+ escHtml\([^)]*\)(?:\.replace\(/\\'/g, \"\"\))? \+ '\\"
    )
    # attr="..." + escHtml( — открытие double-quoted HTML-атрибута
    _RX_ATTR_OPEN = re.compile(r"=\"' \+ escHtml\(")

    def test_no_eschtml_in_js_string_args(self):
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
        hits = self._RX_JS_ARG.findall(html)
        assert hits == [], f"escHtml в JS-string контексте onclick: {hits}"

    def test_no_eschtml_in_attribute_open(self):
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
        hits = self._RX_ATTR_OPEN.findall(html)
        assert hits == [], f"escHtml в attribute-value контексте: {hits}"

    def test_user_controlled_sinks_use_escattr(self):
        """Конкретные исторические раковины (audit file:line) — на escAttr."""
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
        for snippet in (
            "escAttr(r.id)",          # :945 reminder cancel
            "escAttr(r.title)",
            "escAttr(m.id)",          # :1787/:1791 family member
            "escAttr(m.name)",
            "escAttr(detail.id)",     # :1921 recipe delete
            "escAttr(detail.title)",
            "escAttr(member.name)",   # :3077 family edit modal value=
            "escAttr(it.id)",         # :1317/:1321/:1335 shopping
            "escAttr(it.category)",
            "escAttr(currentCategory)",  # :1313 data-shop-cat
            "escAttr(path)",          # :754 dashboard nav
            "escAttr(sk.feature_key)",   # :816 skill nav
            "escAttr(acc.id)",        # :1983/:1985 EDS legacy
            "escAttr(acc.login_masked)",
            'escAttr(sub.cancel_type || "base")',  # :2001
            "escAttr(featureKey)",    # :2057
            "escAttr(skill.plan_key)",   # :2057/:2059
            "escAttr(skill.title)",
            "escAttr(p.plan_key)",    # :2081
            "escAttr(plan.plan_key)",    # :2103
            "escAttr(plan.title)",
        ):
            assert snippet in html, f"ожидался escAttr-синк: {snippet}"

    def test_recipe_source_url_scheme_filtered(self):
        """href из внешнего source_url — только через safeHttpUrl (http/https)."""
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "function safeHttpUrl(" in html
        assert "safeHttpUrl(detail.source_url)" in html
        assert "escAttr(_srcUrl)" in html
        # старый небезопасный вариант не должен вернуться
        assert 'href="\' + escHtml(detail.source_url)' not in html
        assert 'href="\' + escAttr(detail.source_url)' not in html


# ---------------------------------------------------------------------------
# cross-security N3 MINOR: security headers на /miniapp
# ---------------------------------------------------------------------------


class TestMiniAppSecurityHeaders:
    def test_html_page_has_baseline_headers(self, client):
        resp = client.get("/miniapp/")
        assert resp.status_code == 200
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        # HTML-эндпойнт ставит свой no-store — middleware не затирает
        assert "no-store" in resp.headers["Cache-Control"]

    def test_api_json_has_no_store(self, client):
        resp = client.post(
            "/miniapp/api/v1/client-diagnostic", json={"reason": "probe"},
        )
        assert resp.status_code == 200
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# api-admin #2 MINOR: client_diagnostic не логирует сырое body
# ---------------------------------------------------------------------------


class TestClientDiagnosticLogging:
    def test_raw_body_not_logged(self, client, caplog):
        marker_reason = "R" * 5000
        marker_unknown = "UNKNOWNMARKER" * 500
        with caplog.at_level(logging.WARNING, logger="sreda.api.routes.miniapp"):
            resp = client.post(
                "/miniapp/api/v1/client-diagnostic",
                json={
                    "reason": marker_reason,
                    "unknown_blob": marker_unknown,
                    "ua": "test-agent",
                },
            )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        # полные значения — не в логе (known-поля клиппятся, unknown — только ключи)
        assert marker_reason not in caplog.text
        assert marker_unknown not in caplog.text
        # имя unknown-ключа и клипнутый префикс reason — видны для диагностики
        assert "unknown_blob" in caplog.text
        assert "R" * 100 in caplog.text


# ---------------------------------------------------------------------------
# api-admin #6 MINOR: TG lazy-provision IntegrityError race-recovery
# ---------------------------------------------------------------------------


class TestTelegramLazyProvisionRace:
    def test_integrity_error_race_recovers_via_reresolve(self, client, monkeypatch):
        """Проигравший гонщик: ensure падает с IntegrityError, но тенант уже
        создан параллельным запросом → re-resolve → 200 (паритет MAX-ветки)."""
        from sqlalchemy.exc import IntegrityError

        from sreda.api.routes import miniapp as miniapp_module

        real_ensure = miniapp_module.ensure_telegram_user_bundle_by_id

        def _losing_racer(session, **kwargs):
            # «Победитель» гонки уже создал бандл; наш INSERT падает.
            real_ensure(session, **kwargs)
            session.commit()
            raise IntegrityError("INSERT INTO users", {}, Exception("dup"))

        monkeypatch.setattr(
            miniapp_module, "ensure_telegram_user_bundle_by_id", _losing_racer,
        )

        init_data = _make_init_data(user_id=999998)
        resp = client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200

    def test_integrity_error_unrecoverable_returns_500(self, client, monkeypatch):
        """IntegrityError + re-resolve всё ещё пуст → 500 provision_failed_after_race."""
        from sqlalchemy.exc import IntegrityError

        from sreda.api.routes import miniapp as miniapp_module

        def _always_fail(session, **kwargs):
            raise IntegrityError("INSERT INTO users", {}, Exception("dup"))

        monkeypatch.setattr(
            miniapp_module, "ensure_telegram_user_bundle_by_id", _always_fail,
        )

        init_data = _make_init_data(user_id=999997)
        resp = client.get(
            "/miniapp/api/v1/summary",
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "provision_failed_after_race"


# ---------------------------------------------------------------------------
# api-admin #10 MINOR: /subscribe generic is_feature_disabled guard
# ---------------------------------------------------------------------------


class TestSubscribeDisabledFeatureGuard:
    def test_subscribe_disabled_feature_returns_tombstone(
        self, seeded_client, monkeypatch,
    ):
        from sreda.api.routes import miniapp as miniapp_module
        from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
        from sreda.db.session import get_session_factory
        from sreda.services.billing import DISABLED_FEATURE_MESSAGE

        session = get_session_factory()()
        try:
            session.add(SubscriptionPlan(
                id="plan_retired_skill", plan_key="retired_skill_base",
                feature_key="retired_skill", title="Retired", description="",
                price_rub=100, billing_period_days=30,
                is_public=False, is_active=True, sort_order=99,
            ))
            session.commit()
        finally:
            session.close()

        monkeypatch.setattr(
            miniapp_module, "is_feature_disabled",
            lambda feature_key: feature_key == "retired_skill",
        )

        init_data = _make_init_data()
        resp = seeded_client.post(
            "/miniapp/api/v1/subscribe",
            json={"plan_key": "retired_skill_base"},
            headers={"Authorization": f"tma {init_data}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "message": DISABLED_FEATURE_MESSAGE}

        # и никакой мутации — подписка не создана
        session = get_session_factory()()
        try:
            assert (
                session.query(TenantSubscription)
                .filter_by(plan_id="plan_retired_skill")
                .count()
                == 0
            )
        finally:
            session.close()
