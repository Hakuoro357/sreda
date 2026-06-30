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


def _mem_session():
    # Герметичный in-memory сеанс — роут /admin/users иначе дёргает реальный
    # Postgres и тест висит ~264с (Codex R2 субагент). Один движок на сеанс.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()


def test_query_token_bootstrap_redirects_to_clean_url(monkeypatch) -> None:
    # #150 (Codex R3): вход через ?token= на GET → 303 на тот же URL БЕЗ token
    # (cookie уже выставлен) — токен не остаётся в адресной строке/истории/логах.
    # https-база: secure-gate (Codex R4) пропускает редирект только в HTTPS/localhost.
    from sreda.admin.routes import _get_session
    from sreda.config.settings import get_settings

    monkeypatch.setenv("SREDA_ADMIN_TOKEN", "test-admin-tok-123456")
    get_settings.cache_clear()
    app.dependency_overrides[_get_session] = _mem_session
    try:
        client = TestClient(app, base_url="https://testserver")
        r = client.get(
            "/admin/users?token=test-admin-tok-123456", follow_redirects=False,
        )
        assert r.status_code == 303
        assert "token" not in r.headers.get("location", "")
        assert "admin_session=" in r.headers.get("set-cookie", "")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_bootstrap_clean_url_authenticates_via_cookie(monkeypatch) -> None:
    # #150 (Codex R4 high MAJOR): сквозной путь — после 303 чистый URL ДОЛЖЕН
    # аутентифицироваться по выставленному cookie (а не просто «cookie есть»).
    # follow_redirects=True + https → cookie уходит на чистый URL → финальные 200.
    from sreda.admin.routes import _get_session
    from sreda.config.settings import get_settings

    monkeypatch.setenv("SREDA_ADMIN_TOKEN", "test-admin-tok-123456")
    get_settings.cache_clear()
    app.dependency_overrides[_get_session] = _mem_session
    try:
        client = TestClient(app, base_url="https://testserver")
        r = client.get(
            "/admin/users?token=test-admin-tok-123456", follow_redirects=True,
        )
        assert r.status_code == 200                  # чистый URL прошёл по cookie
        assert "token" not in str(r.url)             # токен не остался в адресе
        assert "admin_session=" in r.request.headers.get("cookie", "")  # cookie дошёл
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_plain_http_keeps_token_no_redirect(monkeypatch) -> None:
    # #150 (Codex R4 high MAJOR): на plain-HTTP Secure-cookie не уйдёт обратно →
    # редиректить на чистый URL НЕЛЬЗЯ (иначе 401). Поведение: НЕ редиректим,
    # вход по токену в URL продолжает работать (его прикрывают no-referrer+no-store).
    from sreda.admin.routes import _get_session
    from sreda.config.settings import get_settings

    monkeypatch.setenv("SREDA_ADMIN_TOKEN", "test-admin-tok-123456")
    get_settings.cache_clear()
    app.dependency_overrides[_get_session] = _mem_session
    try:
        # plain-HTTP И на «чужом» хосте, И на localhost (Codex R5 subagent: карв-аут
        # localhost убран — по http://localhost браузер Secure-cookie тоже не шлёт).
        for base in ("http://10.0.0.5", "http://localhost"):
            client = TestClient(app, base_url=base)
            r = client.get(
                "/admin/users?token=test-admin-tok-123456", follow_redirects=False,
            )
            assert r.status_code == 200, base          # отдали страницу, без 303
            assert r.headers.get("Referrer-Policy") == "no-referrer"
            assert "no-store" in r.headers.get("Cache-Control", "")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
