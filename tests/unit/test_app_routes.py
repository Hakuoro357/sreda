from fastapi.testclient import TestClient

from sreda.main import app


def test_eds_monitor_routes_are_not_registered_by_default() -> None:
    client = TestClient(app)
    response = client.get("/api/v1/eds-monitor/status")

    assert response.status_code == 404
