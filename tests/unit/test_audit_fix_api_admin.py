"""Регрессионные тесты фиксов аудита 2026-07-18 — воркер api-admin.

Покрываемые находки (plans/audit-2026-07-18/api-admin-review.md +
svc-security-review.md):

- #8  admin/routes.py — ``admin_tenant_reset`` gated по soft-delete (#187):
      soft-deleted тенант → 303 ``reset=err&msg=tenant_deleted``, данные целы.
- #5  admin/routes.py — rollback-guard в reset: сбой commit'а → честный
      ``reset=err&msg=reset_failed`` + rollback (не ложное «ok»).
- svc-security #5 — ``_login_client_ip``: X-Forwarded-For honoured только от
      доверенного proxy (loopback/private peer).
- #11 admin/csrf.py — fallback-секрет per-process random, не статический
      литерал «sreda-admin-csrf».
- admin-шаблоны — users.html: tenant_name в inline onsubmit только через
      data-атрибут + dataset (Jinja-экранирование не защищает JS-строковый
      контекст); кнопки soft-delete/restore (#7).
- #3 / svc-security #9 — MAX webhook имеет rate-limit dependency (паритет TG).
- #12 api/routes/connect.py — tombstone-роуты без неиспользуемой DB-сессии.
- #9  admin/routes.py — ``Query(pattern=...)`` вместо deprecated ``regex=``.

Без сети и без Postgres: sqlite in-memory + TestClient (StaticPool).
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.db.base import Base
from sreda.db.models.core import OutboxMessage, Tenant

from tests.unit.conftest import seed_telegram_user

_ADMIN_TOKEN = "test-admin-tok-audit-fix"


# ---------------------------------------------------------------------------
# Harness (по образцу test_187_phase4b1_admin_audit.py)
# ---------------------------------------------------------------------------


def _make_engine():
    import sreda.db.models  # noqa: F401 — register core tables
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.billing  # noqa: F401
    import sreda.db.models.inbound_event  # noqa: F401
    import sreda.db.models.skill_platform  # noqa: F401

    eng = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def admin_client(monkeypatch):
    """TestClient боевого app с admin-токеном; /admin-сессия привязана к
    in-memory engine, чтобы тест мог сидировать/инспектировать ту же БД."""
    from sreda.admin.routes import _get_session
    from sreda.config.settings import get_settings
    from sreda.main import app

    monkeypatch.setenv("SREDA_ADMIN_TOKEN", _ADMIN_TOKEN)
    get_settings.cache_clear()

    eng = _make_engine()
    SessionLocal = sessionmaker(bind=eng)

    def _override_session():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[_get_session] = _override_session
    client = TestClient(app, base_url="https://testserver")
    try:
        yield client, SessionLocal
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        eng.dispose()


def _bootstrap_csrf(client) -> str:
    """CSRF-токен, привязанный к admin_session cookie из jar'а клиента."""
    from sreda.admin.csrf import csrf_token

    client.get("/admin/users", headers={"X-Admin-Token": _ADMIN_TOKEN})
    sess = client.cookies.get("admin_session")
    cookies = {"admin_session": sess} if sess else {}
    return csrf_token(SimpleNamespace(cookies=cookies, state=SimpleNamespace()))


def _seed_tenant(session: Session, tid: str, **kw) -> None:
    seed_telegram_user(
        session,
        tenant_id=tid,
        workspace_id=f"ws_{tid}",
        profile=False,
        **kw,
    )
    session.commit()


def _seed_outbox(session: Session, tid: str, row_id: str) -> None:
    session.add(
        OutboxMessage(
            id=row_id,
            tenant_id=tid,
            workspace_id=f"ws_{tid}",
            channel_type="telegram",
            status="pending",
            payload_json="{}",
        )
    )
    session.commit()


def _outbox_count(session: Session, tid: str) -> int:
    return session.query(OutboxMessage).filter_by(tenant_id=tid).count()


# ---------------------------------------------------------------------------
# #8 — reset gated по soft-delete (#187)
# ---------------------------------------------------------------------------


def test_tenant_reset_rejects_soft_deleted_tenant(admin_client) -> None:
    """#187/#8: reset по soft-deleted тенанту → err, данные НЕ удалены."""
    client, SessionLocal = admin_client
    tid = "tenant_reset_deleted"
    with SessionLocal() as s:
        _seed_tenant(s, tid, chat_id="901", user_id="u_reset_deleted")
        _seed_outbox(s, tid, "ob_deleted_1")
        tenant = s.get(Tenant, tid)
        tenant.deleted_at = datetime.now(UTC)
        s.commit()

    r = client.post(
        f"/admin/tenant/reset?tenant_id={tid}",
        headers={"X-Admin-Token": _ADMIN_TOKEN},
        data={"csrf": _bootstrap_csrf(client)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "reset=err" in r.headers["location"]
    assert "msg=tenant_deleted" in r.headers["location"]

    with SessionLocal() as s:
        assert _outbox_count(s, tid) == 1  # ничего не удалено


def test_tenant_reset_active_tenant_still_works(admin_client) -> None:
    """Счастливый путь не сломан: активный тенант → reset=ok, строки удалены,
    audit-запись admin.tenant.reset на месте."""
    from sreda.db.models.audit import AuditLog

    client, SessionLocal = admin_client
    tid = "tenant_reset_ok"
    with SessionLocal() as s:
        _seed_tenant(s, tid, chat_id="902", user_id="u_reset_ok")
        _seed_outbox(s, tid, "ob_ok_1")

    r = client.post(
        f"/admin/tenant/reset?tenant_id={tid}",
        headers={"X-Admin-Token": _ADMIN_TOKEN},
        data={"csrf": _bootstrap_csrf(client)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "reset=ok" in r.headers["location"]

    with SessionLocal() as s:
        assert _outbox_count(s, tid) == 0
        audit = (
            s.query(AuditLog).filter_by(action="admin.tenant.reset", resource_id=tid).all()
        )
        assert len(audit) == 1


def test_tenant_reset_commit_failure_rolls_back_and_reports_err(
    admin_client, monkeypatch
) -> None:
    """#5: сбой commit'а → rollback + честный reset=err (не ложное «ok»),
    строки НЕ потеряны."""
    client, SessionLocal = admin_client
    tid = "tenant_reset_boom"
    with SessionLocal() as s:
        _seed_tenant(s, tid, chat_id="903", user_id="u_reset_boom")
        _seed_outbox(s, tid, "ob_boom_1")

    # Имитация aborted-транзакции / connection blip: commit любой сессии
    # падает. audit_event внутри view проглотит своё исключение (best-effort),
    # а роут reset обязан отработать rollback-guard'ом.
    def _boom_commit(self):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr("sqlalchemy.orm.Session.commit", _boom_commit)

    r = client.post(
        f"/admin/tenant/reset?tenant_id={tid}",
        headers={"X-Admin-Token": _ADMIN_TOKEN},
        data={"csrf": _bootstrap_csrf(client)},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert "reset=err" in r.headers["location"]
    assert "msg=reset_failed" in r.headers["location"]

    with SessionLocal() as s:
        assert _outbox_count(s, tid) == 1  # rollback сохранил строки


# ---------------------------------------------------------------------------
# svc-security #5 — XFF только за доверенным proxy
# ---------------------------------------------------------------------------


def _fake_request(peer: str | None, xff: str | None = None):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=peer) if peer else None,
    )


def test_login_client_ip_ignores_xff_from_public_peer() -> None:
    from sreda.admin.routes import _login_client_ip

    req = _fake_request("1.2.3.4", "9.9.9.9, 10.0.0.1")
    assert _login_client_ip(req) == "1.2.3.4"


def test_login_client_ip_honors_xff_from_loopback_proxy() -> None:
    from sreda.admin.routes import _login_client_ip

    req = _fake_request("127.0.0.1", "9.9.9.9, 10.0.0.1")
    assert _login_client_ip(req) == "9.9.9.9"


def test_login_client_ip_honors_xff_from_private_proxy() -> None:
    from sreda.admin.routes import _login_client_ip

    req = _fake_request("10.0.0.8", "9.9.9.9")
    assert _login_client_ip(req) == "9.9.9.9"


def test_login_client_ip_non_ip_peer_does_not_trust_xff() -> None:
    """TestClient-подобный peer («testclient») — не IP → XFF игнорируется."""
    from sreda.admin.routes import _login_client_ip

    req = _fake_request("testclient", "9.9.9.9")
    assert _login_client_ip(req) == "testclient"


def test_login_client_ip_no_client_no_xff() -> None:
    from sreda.admin.routes import _login_client_ip

    assert _login_client_ip(_fake_request(None)) == "?"


# ---------------------------------------------------------------------------
# #11 — CSRF fallback-секрет: per-process random, не статический литерал
# ---------------------------------------------------------------------------


def test_csrf_fallback_secret_is_random_not_static(monkeypatch) -> None:
    from sreda.admin import csrf as csrf_mod

    monkeypatch.setattr(
        csrf_mod,
        "get_settings",
        lambda: SimpleNamespace(encryption_key=None, admin_token=None),
    )
    monkeypatch.setattr(csrf_mod, "_FALLBACK_SECRET", None)
    monkeypatch.setattr(csrf_mod, "_WARNED_NO_SECRET", False)

    req = SimpleNamespace(cookies={}, state=SimpleNamespace())
    t1 = csrf_mod.csrf_token(req)
    t2 = csrf_mod.csrf_token(req)
    assert t1 == t2  # стабилен внутри процесса
    # …но НЕ детерминированный cross-deploy литерал «sreda-admin-csrf».
    legacy = hmac.new(b"sreda-admin-csrf", b"anon", hashlib.sha256).hexdigest()
    assert t1 != legacy


# ---------------------------------------------------------------------------
# admin-шаблоны: escAttr в inline-атрибутах + soft-delete/restore UI (#7)
# ---------------------------------------------------------------------------


def test_users_page_no_tenant_name_interpolation_in_inline_handlers(
    admin_client,
) -> None:
    """tenant_name (user-controlled TG first/last name) НЕ интерполируется в
    JS-строковый контекст onsubmit — только data-атрибут + dataset."""
    client, SessionLocal = admin_client
    tid = "tenant_xss_name"
    with SessionLocal() as s:
        _seed_tenant(
            s, tid, chat_id="904", user_id="u_xss", tenant_name="O'Brien"
        )

    r = client.get("/admin/users", headers={"X-Admin-Token": _ADMIN_TOKEN})
    assert r.status_code == 200
    # Имя живёт в data-атрибуте (Jinja-экранирование валидно для HTML-атрибута)…
    assert 'data-name="O&#39;Brien"' in r.text
    # …а confirm() собирает строку через dataset, БЕЗ интерполяции имени.
    assert "this.dataset.name" in r.text
    assert "Сбросить tenant O&#39;" not in r.text
    assert "Приостановить tenant O&#39;" not in r.text


def test_users_page_soft_delete_and_restore_buttons(admin_client) -> None:
    """#7: активный тенант → форма soft-delete; удалённый → форма restore."""
    client, SessionLocal = admin_client
    with SessionLocal() as s:
        _seed_tenant(s, "tenant_sd_active", chat_id="905", user_id="u_sd_a")
        _seed_tenant(s, "tenant_sd_deleted", chat_id="906", user_id="u_sd_d")
        tenant = s.get(Tenant, "tenant_sd_deleted")
        tenant.deleted_at = datetime.now(UTC)
        s.commit()

    r = client.get("/admin/users", headers={"X-Admin-Token": _ADMIN_TOKEN})
    assert r.status_code == 200
    assert 'action="/admin/tenant/tenant_sd_active/soft-delete"' in r.text
    assert 'action="/admin/tenant/tenant_sd_deleted/restore"' in r.text
    # Для удалённого тенанта мутации (reset/soft-delete) не показываем.
    assert 'action="/admin/tenant/tenant_sd_deleted/soft-delete"' not in r.text


# ---------------------------------------------------------------------------
# #3 / svc-security #9 — MAX webhook rate-limit
# ---------------------------------------------------------------------------


def test_max_webhook_has_rate_limit_dependency_first() -> None:
    from sreda.api.deps import enforce_max_rate_limit
    from sreda.api.routes import max_webhook as mw

    route = next(
        r for r in mw.router.routes if getattr(r, "path", "") == "/api/max/webhook"
    )
    deps = [getattr(d, "dependency", None) for d in route.dependencies]
    # Порядок как у TG: rate-limit первым (дёшево), затем secret-check.
    assert deps[0] is enforce_max_rate_limit
    assert mw._verify_max_secret in deps


def test_enforce_max_rate_limit_raises_429_when_exceeded(monkeypatch) -> None:
    from sreda.api import deps
    from sreda.services.rate_limiter import InMemoryRateLimiter

    deps.reset_rate_limiters()
    limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60.0)
    monkeypatch.setattr(deps, "_max_limiter", lambda: limiter)
    req = SimpleNamespace(client=SimpleNamespace(host="9.9.9.9"))
    deps.enforce_max_rate_limit(req)  # первый проходит
    with pytest.raises(HTTPException) as exc:
        deps.enforce_max_rate_limit(req)
    assert exc.value.status_code == 429


# ---------------------------------------------------------------------------
# #12 — connect tombstones без DB-сессии; #9 — Query(pattern=)
# ---------------------------------------------------------------------------


def test_connect_tombstones_do_not_open_db_session() -> None:
    from sreda.api.routes import connect

    assert "session" not in inspect.signature(
        connect.open_eds_connect_form
    ).parameters
    assert "session" not in inspect.signature(
        connect.submit_eds_connect_form
    ).parameters


def test_traces_user_id_query_uses_pattern_not_regex() -> None:
    from sreda.admin import routes as admin_routes

    default = inspect.signature(admin_routes.admin_traces).parameters[
        "user_id"
    ].default
    # FastAPI 0.139: pattern хранится в metadata (_PydanticGeneralMetadata).
    patterns = [getattr(m, "pattern", None) for m in default.metadata]
    assert r"^[a-zA-Z0-9_]*$" in patterns
