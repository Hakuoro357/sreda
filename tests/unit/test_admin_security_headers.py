"""#150 CRITICAL-митигейт: /admin-ответы несут no-referrer + no-store."""
from __future__ import annotations

from fastapi.testclient import TestClient

from sreda.main import app


def test_admin_response_has_security_headers() -> None:
    client = TestClient(app)
    # без токена → 401/403, но middleware всё равно вешает заголовки на /admin.
    r = client.get("/admin/users")
    assert r.status_code in (401, 403)
    assert r.headers.get("Referrer-Policy") == "no-referrer"
    assert "no-store" in r.headers.get("Cache-Control", "")


def test_non_admin_path_not_forced() -> None:
    client = TestClient(app)
    r = client.get("/health")
    # health не /admin → no-referrer middleware не навязывает
    assert r.headers.get("Referrer-Policy") != "no-referrer"


def test_query_token_bootstrap_redirects_to_clean_url(monkeypatch) -> None:
    # #150 (Codex R3): вход через ?token= на GET → 303 на тот же URL БЕЗ token
    # (cookie уже выставлен) — токен не остаётся в адресной строке/истории/логах.
    # Герметично: override _get_session на in-memory (роут /admin/users иначе
    # дёргает реальный Postgres и тест висит — Codex R2 субагент).
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.admin.routes import _get_session
    from sreda.config.settings import get_settings
    from sreda.db.base import Base

    monkeypatch.setenv("SREDA_ADMIN_TOKEN", "test-admin-tok-123456")
    get_settings.cache_clear()

    def _mem_session():
        eng = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(eng)
        s = sessionmaker(bind=eng)()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[_get_session] = _mem_session
    try:
        client = TestClient(app)
        r = client.get(
            "/admin/users?token=test-admin-tok-123456", follow_redirects=False,
        )
        assert r.status_code == 303
        assert "token" not in r.headers.get("location", "")
        assert "admin_session=" in r.headers.get("set-cookie", "")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
