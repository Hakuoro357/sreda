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
