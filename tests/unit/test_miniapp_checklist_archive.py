"""R-31 (2026-05-15): POST /miniapp/api/v1/checklist/{id}/archive endpoint tests.

User feedback (tg_755682022): «дать возможность удалять списки дел...
иконка слева в титул списка. Удалять с подтверждением без перезагрузки».

Backend: soft-archive через `ChecklistService.archive_list` (status='archived').
Idempotent — already-archived returns 200 `{"ok": True, "already_archived": True}`.

Tests:
- Happy path: active checklist → archived (status changes in DB).
- Idempotent: already-archived → 200 с `already_archived: true`.
- Ownership guard: wrong tenant/user → 404.
- Non-existent checklist → 404.
- Auth: missing/invalid signature → 401 (covered by existing _require_miniapp_auth tests).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from sreda.main import create_app

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


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
    """TestClient + in-memory SQLite. Mirrors test_miniapp_api.client."""
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

    with TestClient(create_app()) as c:
        yield c

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    reset_rate_limiters()


@pytest.fixture()
def seeded_with_checklist(client, monkeypatch):
    """Seed: tenant + user + 1 active checklist with items. Returns (client, checklist_id, init_data)."""
    from sreda.db.session import get_session_factory
    from sreda.db.repositories.seed import SeedRepository
    from sreda.services.checklists import ChecklistService

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
            eds_monitor_enabled=False,
        )
        session.commit()
        svc = ChecklistService(session)
        cl = svc.create_list(
            tenant_id="tenant_test", user_id="user_test", title="Тест-список"
        )
        svc.add_items(list_id=cl.id, items=["Пункт A", "Пункт B"])
        checklist_id = cl.id
    finally:
        session.close()

    init_data = _make_init_data()
    return client, checklist_id, init_data


def test_archive_active_checklist_returns_ok(seeded_with_checklist) -> None:
    """Happy path: POST /archive на active checklist → 200 ok, status='archived' в DB."""
    client, checklist_id, init_data = seeded_with_checklist

    resp = client.post(
        f"/miniapp/api/v1/checklist/{checklist_id}/archive",
        headers={"Authorization": f"tma {init_data}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["already_archived"] is False

    # Verify DB state changed
    from sreda.db.session import get_session_factory
    from sreda.db.models.checklists import Checklist, ChecklistItem
    session = get_session_factory()()
    try:
        cl = session.query(Checklist).filter(Checklist.id == checklist_id).one()
        assert cl.status == "archived"
        # Qwen R1 MINOR: items должны сохраниться в БД (soft-archive
        # semantics: данные не теряются, status changes только у parent).
        items = session.query(ChecklistItem).filter(
            ChecklistItem.checklist_id == checklist_id
        ).all()
        assert len(items) == 2, "Items должны сохраниться после soft-archive"
    finally:
        session.close()


def test_archive_idempotent_already_archived(seeded_with_checklist) -> None:
    """Idempotent: second POST на already-archived → 200 с already_archived=True."""
    client, checklist_id, init_data = seeded_with_checklist

    # First archive
    r1 = client.post(
        f"/miniapp/api/v1/checklist/{checklist_id}/archive",
        headers={"Authorization": f"tma {init_data}"},
    )
    assert r1.status_code == 200
    assert r1.json()["already_archived"] is False

    # Second archive — idempotent
    r2 = client.post(
        f"/miniapp/api/v1/checklist/{checklist_id}/archive",
        headers={"Authorization": f"tma {init_data}"},
    )
    assert r2.status_code == 200
    assert r2.json()["ok"] is True
    assert r2.json()["already_archived"] is True


def test_archive_nonexistent_checklist_returns_404(seeded_with_checklist) -> None:
    """Non-existent ID → 404."""
    client, _, init_data = seeded_with_checklist

    resp = client.post(
        "/miniapp/api/v1/checklist/checklist_nonexistent_id/archive",
        headers={"Authorization": f"tma {init_data}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "checklist_not_found"


def test_archive_wrong_tenant_returns_404(client, monkeypatch) -> None:
    """Ownership guard: checklist принадлежит другому tenant → 404 (не leakable как 403)."""
    from sreda.db.session import get_session_factory
    from sreda.db.repositories.seed import SeedRepository
    from sreda.services.checklists import ChecklistService

    # Seed two tenants. First — calling user. Second — owns the checklist.
    session = get_session_factory()()
    try:
        SeedRepository(session).ensure_tenant_bundle(
            tenant_id="tenant_caller",
            tenant_name="Caller",
            workspace_id="ws_caller",
            workspace_name="Caller WS",
            user_id="user_caller",
            telegram_account_id="111",
            assistant_id="assistant_caller",
            assistant_name="Среда",
            eds_monitor_enabled=False,
        )
        SeedRepository(session).ensure_tenant_bundle(
            tenant_id="tenant_owner",
            tenant_name="Owner",
            workspace_id="ws_owner",
            workspace_name="Owner WS",
            user_id="user_owner",
            telegram_account_id="222",
            assistant_id="assistant_owner",
            assistant_name="Среда",
            eds_monitor_enabled=False,
        )
        session.commit()
        svc = ChecklistService(session)
        cl = svc.create_list(
            tenant_id="tenant_owner", user_id="user_owner", title="Owner's list"
        )
        owner_checklist_id = cl.id
    finally:
        session.close()

    # Caller (different tenant) tries to archive owner's checklist.
    caller_init_data = _make_init_data(user_id=111)
    resp = client.post(
        f"/miniapp/api/v1/checklist/{owner_checklist_id}/archive",
        headers={"Authorization": f"tma {caller_init_data}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "checklist_not_found"

    # Verify owner's checklist still active
    session = get_session_factory()()
    try:
        from sreda.db.models.checklists import Checklist
        cl = session.query(Checklist).filter(Checklist.id == owner_checklist_id).one()
        assert cl.status == "active"  # untouched
    finally:
        session.close()


def test_archive_no_auth_returns_401(client) -> None:
    """Missing auth header → 401 (через _require_miniapp_auth)."""
    resp = client.post("/miniapp/api/v1/checklist/cl_any/archive")
    assert resp.status_code == 401
