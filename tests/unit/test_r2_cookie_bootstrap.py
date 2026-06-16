import pytest
from fastapi.testclient import TestClient

@pytest.fixture()
def client(monkeypatch):
    from sreda.config.settings import Settings, get_settings
    import sreda.admin.auth as auth
    monkeypatch.setattr(auth, "get_settings", lambda: Settings(admin_token="secret-xyz"))
    from sreda.main import app
    # TestClient does NOT send Secure cookies over http by default? It stores them; we test cookie roundtrip.
    return TestClient(app)

def test_cookie_set_on_first_token_auth(client):
    # First load WITH token -> should set admin_session cookie (Set-Cookie header)
    r = client.get("/admin/users?token=secret-xyz", follow_redirects=False)
    # may be 200 or error depending on DB, but auth must pass and cookie be set
    sc = r.headers.get("set-cookie", "")
    print("status:", r.status_code, "set-cookie:", sc[:80])
    assert "admin_session=" in sc, f"no cookie set; status={r.status_code} body={r.text[:200]}"
    assert "HttpOnly" in sc
    assert "samesite=strict" in sc.lower()

def test_tokenless_nav_after_cookie(client):
    # 1) authenticate with token (sets cookie in client jar)
    r1 = client.get("/admin/users?token=secret-xyz")
    assert "admin_session=" in r1.headers.get("set-cookie", ""), r1.status_code
    # 2) now navigate token-less; client jar replays the cookie
    r2 = client.get("/admin/users")
    print("tokenless nav status:", r2.status_code)
    assert r2.status_code not in (401, 403), f"token-less nav rejected: {r2.status_code}"

def test_cold_tokenless_first_load_is_401(client):
    # bootstrap: very first request token-less, no cookie -> 401
    r = client.get("/admin/budget")
    assert r.status_code == 401, r.status_code
