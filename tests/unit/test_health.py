from fastapi.testclient import TestClient

from sreda.main import app


def test_health_live() -> None:
    client = TestClient(app)
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
