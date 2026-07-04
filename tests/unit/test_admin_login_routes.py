"""#305 — admin login web routes + CSRF gate (route-level).

Named tests for the acceptance checklist:

- CSRF (п.7): an admin POST without a csrf field → 403; with a cross-site
  Sec-Fetch-Site header → 403; with the valid token → passes (fail-closed on
  the ROUTE, not the template).
- Login flow (Phase 3): GET /admin/login sets the two Strict/HttpOnly cookies
  and renders the deep-link + human_code (no secret in the deep-link beyond the
  public challenge_id); GET /admin/login/status is read-only; POST
  /admin/login/claim consumes a confirmed challenge and mints the session cookie.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.admin import routes as admin_routes
from sreda.admin.auth import AdminPrincipal, require_admin_token
from sreda.admin.csrf import csrf_token
from sreda.db.base import Base
import sreda.db.models.admin_auth  # noqa: F401
from sreda.services import admin_login as al


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import sreda.db.models.audit  # noqa: F401 — audit_log used by lazy audit
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def client(session_factory, monkeypatch):
    app = FastAPI()
    app.include_router(admin_routes.router)

    def _session_override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[require_admin_token] = lambda: AdminPrincipal("token")
    app.dependency_overrides[admin_routes._get_session] = _session_override
    return TestClient(app)


# --------------------------------------------------------------------- CSRF

def test_admin_post_without_csrf_rejected(client, monkeypatch):
    """CSRF fail-closed: POST без csrf-поля → 403 (не выполняет действие)."""
    import sreda.admin.overview_snapshot as ov
    monkeypatch.setattr(ov, "refresh_overview", lambda *a, **k: True)
    resp = client.post("/admin/refresh-snapshot", follow_redirects=False)
    assert resp.status_code == 403


def test_admin_post_cross_site_sec_fetch_rejected(client, monkeypatch):
    """CSRF: явный cross-site Sec-Fetch-Site → 403 даже с валидным токеном."""
    import sreda.admin.overview_snapshot as ov
    monkeypatch.setattr(ov, "refresh_overview", lambda *a, **k: True)
    # Build a valid token for an anon binding (no session cookie present).
    token = csrf_token(SimpleNamespace(cookies={}))
    resp = client.post(
        "/admin/refresh-snapshot",
        data={"csrf": token},
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_admin_post_with_valid_csrf_passes(client, monkeypatch):
    """CSRF: валидный токен (anon-binding) + same-origin → 303 (действие идёт)."""
    called = {}
    import sreda.admin.overview_snapshot as ov
    monkeypatch.setattr(
        ov, "refresh_overview",
        lambda sf, st: called.setdefault("yes", True) or True,
    )
    token = csrf_token(SimpleNamespace(cookies={}))
    resp = client.post(
        "/admin/refresh-snapshot",
        data={"csrf": token},
        headers={"sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert called.get("yes") is True
    # #305: the redirect target must NOT carry a raw token.
    assert "token=" not in resp.headers["location"]


# ------------------------------------------------------------- login flow

def _settings_for_login(monkeypatch):
    """Patch routes.get_settings so bot username + admin_bot_key resolve, and
    TelegramBotRegistry returns a bot with a username."""
    st = SimpleNamespace(
        admin_bot_key="sreda",
        admin_tg_ids=frozenset({"42"}),  # non-empty → login enabled
        telegram_bot_token="tg-tok",
        telegram_bot_username="sreda01_bot",
        telegram_miniapp_shortname="sreda_app",
        home_bot_token=None,
        home_bot_username=None,
        home_miniapp_shortname=None,
        home_bot_signup_open=True,
        system_default_bot_key="sreda",
    )
    monkeypatch.setattr(admin_routes, "get_settings", lambda: st)


def test_get_login_sets_cookies_and_renders(client, monkeypatch):
    _settings_for_login(monkeypatch)
    resp = client.get("/admin/login", follow_redirects=False)
    assert resp.status_code == 200
    # deep-link with the public challenge_id (adm_ prefix), human_code shown.
    assert "t.me/sreda01_bot?start=adm_" in resp.text
    # both login cookies set (Strict / HttpOnly).
    setcookies = resp.headers.get_list("set-cookie")
    joined = " ".join(setcookies)
    assert "challenge_ref=" in joined
    assert "browser_bind=" in joined
    assert joined.lower().count("httponly") >= 2


def test_status_is_read_only(client, monkeypatch, session_factory):
    """GET /admin/login/status не логинит и не мутирует: pending остаётся pending."""
    _settings_for_login(monkeypatch)
    # Seed a pending challenge and set the browser cookies manually.
    s = session_factory()
    r = al.start_challenge(s, "1.2.3.4")
    s.close()
    client.cookies.set("challenge_ref", r.challenge_id)
    client.cookies.set("browser_bind", r.browser_bind_raw)
    resp = client.get("/admin/login/status")
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}
    # Still pending after polling — no mutation, no cookie minted.
    s = session_factory()
    assert al.get_status(s, r.challenge_id, r.browser_bind_raw) == "pending"
    s.close()


def test_claim_confirmed_mints_session_cookie(client, monkeypatch, session_factory):
    _settings_for_login(monkeypatch)
    s = session_factory()
    r = al.start_challenge(s, "1.2.3.4")
    al.attach_bot(s, r.challenge_id, "42", "sreda", "chat1")
    al.confirm(s, r.challenge_id, "42")
    s.close()

    client.cookies.set("challenge_ref", r.challenge_id)
    client.cookies.set("browser_bind", r.browser_bind_raw)
    resp = client.post(
        "/admin/login/claim",
        headers={"sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert resp.json() == {"redirect": "/admin"}
    joined = " ".join(resp.headers.get_list("set-cookie"))
    assert "admin_tg_session=" in joined


def test_claim_wrong_bind_denied_not_burned(client, monkeypatch, session_factory):
    _settings_for_login(monkeypatch)
    s = session_factory()
    r = al.start_challenge(s, "1.2.3.4")
    al.attach_bot(s, r.challenge_id, "42", "sreda", "chat1")
    al.confirm(s, r.challenge_id, "42")
    s.close()

    client.cookies.set("challenge_ref", r.challenge_id)
    client.cookies.set("browser_bind", "WRONG-BIND")
    resp = client.post(
        "/admin/login/claim",
        headers={"sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    # Not burned: still confirmed for the correct bind.
    s = session_factory()
    assert al.get_status(s, r.challenge_id, r.browser_bind_raw) == "confirmed"
    s.close()


# --------------------------------------------------- Item C: claim same-origin

@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site"},
        {"origin": "https://evil.example", "host": "testserver"},
    ],
    ids=["cross-site-secfetch", "foreign-origin"],
)
def test_claim_cross_site_rejected(client, monkeypatch, session_factory, headers):
    """#305 checklist #10: /admin/login/claim с чужим Origin / cross-site
    Sec-Fetch-Site → 403 на РОУТЕ (require_same_origin), ДО claim."""
    _settings_for_login(monkeypatch)
    s = session_factory()
    r = al.start_challenge(s, "1.2.3.4")
    al.attach_bot(s, r.challenge_id, "42", "sreda", "chat1")
    al.confirm(s, r.challenge_id, "42")
    s.close()

    client.cookies.set("challenge_ref", r.challenge_id)
    client.cookies.set("browser_bind", r.browser_bind_raw)
    resp = client.post("/admin/login/claim", headers=headers, follow_redirects=False)
    assert resp.status_code == 403
    # NOT burned — the confirmed challenge survives a rejected claim.
    s = session_factory()
    assert al.get_status(s, r.challenge_id, r.browser_bind_raw) == "confirmed"
    s.close()


# --------------------------------------------- Item B: empty-allowlist login

def test_login_fail_closed_when_allowlist_empty(client, monkeypatch):
    """#305 checklist #15/#10: пустой SREDA_ADMIN_TG_IDS → /admin/login НЕ
    рендерит мёртвую кнопку, а fail-closed 403."""
    st = SimpleNamespace(
        admin_bot_key="sreda",
        admin_tg_ids=frozenset(),  # empty allowlist
        telegram_bot_token="tg-tok",
        telegram_bot_username="sreda01_bot",
        telegram_miniapp_shortname="sreda_app",
        home_bot_token=None, home_bot_username=None, home_miniapp_shortname=None,
        home_bot_signup_open=True, system_default_bot_key="sreda",
    )
    monkeypatch.setattr(admin_routes, "get_settings", lambda: st)
    resp = client.get("/admin/login", follow_redirects=False)
    assert resp.status_code == 403
    assert "start=adm_" not in resp.text  # no dead deep-link button


# --------------------------- Item E: CSRF gate parametrized over ALL POSTs

# Every state-changing admin POST carrying require_csrf (grep @router.post).
_CSRF_POST_ROUTES = [
    "/admin/refresh-snapshot",
    "/admin/llm",
    "/admin/tenant/reset?tenant_id=t1",
    "/admin/tenant/t1/suspend",
    "/admin/tenant/t1/unsuspend",
    "/admin/tenant/t1/soft-delete",
    "/admin/tenant/t1/restore",
]


@pytest.mark.parametrize("route", _CSRF_POST_ROUTES)
def test_every_admin_post_rejects_without_csrf(client, route):
    """Anti-omission: КАЖДЫЙ state-changing admin POST без csrf-поля → 403
    (require_csrf fail-closed на РОУТЕ, до тела хендлера)."""
    resp = client.post(route, follow_redirects=False)
    assert resp.status_code == 403, f"{route} did not fail-closed: {resp.status_code}"


@pytest.mark.parametrize("route", _CSRF_POST_ROUTES)
def test_every_admin_post_rejects_cross_site_even_with_token(client, route):
    """Каждый admin POST с валидным токеном НО cross-site Sec-Fetch → 403."""
    token = csrf_token(SimpleNamespace(cookies={}))
    resp = client.post(
        route, data={"csrf": token},
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert resp.status_code == 403, f"{route} accepted cross-site: {resp.status_code}"


# ============================================================================
# Real-app tests (middleware + exception handler): Item B disabled-403, Item D
# fallback-token bootstrap CSRF.
# ============================================================================

_ADMIN_TOKEN_RA = "ra-admin-tok-abcdef"


@pytest.fixture()
def real_app_client(monkeypatch):
    """TestClient over the REAL app (so the admin middleware + 401→login
    exception handler are exercised). Admin token set; /admin route session
    bound to a shared in-memory engine."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from sreda.admin.routes import _get_session
    from sreda.config.settings import get_settings
    from sreda.main import app

    monkeypatch.setenv("SREDA_ADMIN_TOKEN", _ADMIN_TOKEN_RA)
    monkeypatch.delenv("SREDA_ADMIN_TG_IDS", raising=False)
    get_settings.cache_clear()

    import sreda.db.models  # noqa: F401
    import sreda.db.models.audit  # noqa: F401
    eng = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng)

    def _override():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[_get_session] = _override
    client = TestClient(app, base_url="https://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        eng.dispose()


def test_admin_disabled_returns_403_not_redirect(monkeypatch):
    """#305 checklist #15: оба env пусты (нет токена И нет allowlist) → GET /admin
    отдаёт 403 (НЕ 302 на /admin/login — редиректить некуда)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from sreda.admin.routes import _get_session
    from sreda.config.settings import get_settings
    from sreda.main import app

    monkeypatch.delenv("SREDA_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SREDA_ADMIN_TG_IDS", raising=False)
    get_settings.cache_clear()

    import sreda.db.models  # noqa: F401
    import sreda.db.models.audit  # noqa: F401
    eng = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng)
    app.dependency_overrides[_get_session] = lambda: iter([SessionLocal()])
    try:
        client = TestClient(app, base_url="https://testserver")
        resp = client.get(
            "/admin/", headers={"accept": "text/html"}, follow_redirects=False,
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        eng.dispose()


def test_fallback_token_bootstrap_csrf_matches(real_app_client):
    """Item D: первый GET по X-Admin-Token ставит admin_session-cookie в ОТВЕТЕ;
    отрендеренный csrf должен биндиться к pending-сессии, чтобы следующий POST
    (уже с cookie) прошёл. Без фикса: form-csrf(anon) ≠ csrf(cookie) → 403."""
    import re

    import sreda.admin.overview_snapshot as ov
    import sreda.main  # noqa: F401

    # GET the dashboard authenticated via header token — form is rendered and the
    # admin_session cookie is set on THIS response by the middleware.
    r = real_app_client.get(
        "/admin/", headers={"X-Admin-Token": _ADMIN_TOKEN_RA},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.status_code
    # Extract the csrf token the server put in the refresh-snapshot form.
    m = re.search(r'name="csrf" value="([0-9a-f]{64})"', r.text)
    assert m, "csrf hidden field not found in rendered form"
    form_csrf = m.group(1)
    # The TestClient cookie jar now holds admin_session (set by the response).
    assert real_app_client.cookies.get("admin_session")

    # Now POST refresh-snapshot with the form token — the request carries the
    # admin_session cookie, so csrf_token(request) must equal the rendered one.
    called = {}
    import sreda.admin.overview_snapshot as _ov
    orig = _ov.refresh_overview
    _ov.refresh_overview = lambda sf, st: called.setdefault("y", True) or True
    try:
        resp = real_app_client.post(
            "/admin/refresh-snapshot",
            data={"csrf": form_csrf},
            headers={"X-Admin-Token": _ADMIN_TOKEN_RA, "sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
    finally:
        _ov.refresh_overview = orig
    assert resp.status_code == 303, f"bootstrap CSRF mismatch → {resp.status_code}"
    assert called.get("y") is True
