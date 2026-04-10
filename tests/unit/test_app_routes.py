from fastapi.testclient import TestClient

from sreda.main import app


def test_eds_monitor_routes_are_not_registered_by_default() -> None:
    client = TestClient(app)
    response = client.get("/api/v1/eds-monitor/status")

    assert response.status_code == 404


def test_approvals_endpoint_requires_authentication() -> None:
    """The approvals endpoint is a stub today, but once it starts
    returning real data it must be gated behind authentication. We
    fail-closed now so a future PR adding a real handler cannot
    accidentally leak data through this public route.
    """

    client = TestClient(app)
    response = client.get("/api/v1/approvals")

    assert response.status_code == 401
