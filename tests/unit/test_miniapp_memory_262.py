"""#262 срезы B+C — API мини-аппа для категорий и фактов памяти (endpoint-уровень).

Мини-апп УПРАВЛЯЕТ категориями (CRUD), показывает факты по ярусам и поддерживает ПРАВКУ/УДАЛЕНИЕ факта
(решение владельца 2026-06-30 — вернули из «только голос»; правка пере-эмбеддит, 503 если эмбеддер недоступен).
Перенос факта между категориями — остаётся голосом/текстом через бот.

Гоняет через TestClient(create_app()) с реальной auth (Telegram initData) + SQLite, где get_db_session ставит
PRAGMA foreign_keys=ON → composite FK/каскад энфорсятся в API-пути. Покрывает: маппинг ошибок (404/403/409/400/422),
DTO-allowlist (нет утечки slug/name_normalized/embedding), изоляцию тенантов.
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
from sreda.services.embeddings import FakeEmbeddingClient

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def _make_init_data(*, user_id: int = 352612382, auth_date: int | None = None) -> str:
    if auth_date is None:
        auth_date = int(time.time())
    user_json = json.dumps(
        {"id": user_id, "first_name": "Test", "username": "t"}, separators=(",", ":")
    )
    params = {"auth_date": str(auth_date), "user": user_json}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)


def _headers() -> dict:
    return {"Authorization": f"tma {_make_init_data()}"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.test.local")

    from sreda.api.deps import reset_rate_limiters
    from sreda.config.settings import get_settings
    from sreda.db.base import Base
    from sreda.db.session import get_engine, get_session_factory

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
def seeded(client):
    from sreda.db.repositories.seed import SeedRepository
    from sreda.db.session import get_session_factory

    s = get_session_factory()()
    try:
        SeedRepository(s).ensure_tenant_bundle(
            tenant_id="tenant_test", tenant_name="T", workspace_id="ws", workspace_name="W",
            user_id="user_test", telegram_account_id="352612382", assistant_id="as",
            assistant_name="С", eds_monitor_enabled=False)
        s.commit()
    finally:
        s.close()
    return client


def _save_fact(content: str, category_id: str | None = None, *, tier: str = "core") -> str:
    """Факт напрямую через репо (в мини-аппе нет create-фактов — они от агента/бота)."""
    from sreda.db.repositories.memory import MemoryRepository
    from sreda.db.session import get_session_factory

    s = get_session_factory()()
    try:
        m = MemoryRepository(s).save(
            "tenant_test", "user_test", tier=tier, content=content, embedding=[0.1, 0.2],
            category_id=category_id)
        s.commit()
        return m.id
    finally:
        s.close()


def _seed_other_user_category() -> str:
    """Второй тенант/юзер + его категория. Возвращает id чужой категории (для проверки изоляции)."""
    from sreda.db.repositories.memory import MemoryRepository
    from sreda.db.repositories.seed import SeedRepository
    from sreda.db.session import get_session_factory

    s = get_session_factory()()
    try:
        SeedRepository(s).ensure_tenant_bundle(
            tenant_id="tenant_other", tenant_name="O", workspace_id="wso", workspace_name="WO",
            user_id="user_other", telegram_account_id="999000", assistant_id="aso",
            assistant_name="O", eds_monitor_enabled=False)
        s.commit()
        cat = MemoryRepository(s).create_category("tenant_other", "user_other", "Чужая")
        s.commit()
        return cat.id
    finally:
        s.close()


# --- menu tile (срез C) ---------------------------------------------------------

def test_memory_tile_in_menu(seeded):
    """C: плитка «Память» (платформенная) присутствует в меню мини-аппа с роутом #/memory."""
    resp = seeded.get("/miniapp/api/v1/menu", headers=_headers())
    assert resp.status_code == 200
    items = resp.json()["items"]
    mem = [it for it in items if it.get("id") == "memory"]
    assert len(mem) == 1
    assert mem[0]["route"] == "#/memory"
    assert mem[0]["title"] == "Память"


# --- categories CRUD ------------------------------------------------------------

def test_list_categories_lazily_creates_common_and_hides_internal_fields(seeded):
    resp = seeded.get("/miniapp/api/v1/memory/categories", headers=_headers())
    assert resp.status_code == 200
    cats = resp.json()["categories"]
    assert len(cats) == 1 and cats[0]["is_system"] is True  # Common создана лениво
    assert set(cats[0]) == {"id", "name", "is_system", "fact_count"}  # DTO-allowlist
    assert "slug" not in cats[0] and "name_normalized" not in cats[0]


def test_create_category_ok_and_duplicate_409(seeded):
    r1 = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "Работа"}, headers=_headers())
    assert r1.status_code == 200 and r1.json()["category"]["name"] == "Работа"
    r2 = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "работа"}, headers=_headers())
    assert r2.status_code == 409 and r2.json()["detail"] == "category_name_conflict"


def test_create_reserved_common_name_409(seeded):
    r = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "Общее"}, headers=_headers())
    assert r.status_code == 409  # зарезервировано за системной Common


def test_create_empty_name_400(seeded):
    r = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "   "}, headers=_headers())
    assert r.status_code == 400


def test_create_name_too_long_422(seeded):
    r = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "я" * 200}, headers=_headers())
    assert r.status_code == 422  # Pydantic max_length


def test_rename_common_is_403(seeded):
    common_id = seeded.get("/miniapp/api/v1/memory/categories",
                           headers=_headers()).json()["categories"][0]["id"]
    r = seeded.patch(f"/miniapp/api/v1/memory/categories/{common_id}", json={"name": "Х"}, headers=_headers())
    assert r.status_code == 403 and r.json()["detail"] == "category_immutable"


def test_rename_ok_and_not_found_404(seeded):
    cat_id = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "Работа"},
                         headers=_headers()).json()["category"]["id"]
    assert seeded.patch(f"/miniapp/api/v1/memory/categories/{cat_id}", json={"name": "Хобби"},
                        headers=_headers()).status_code == 200
    assert seeded.patch("/miniapp/api/v1/memory/categories/nope", json={"name": "Х"},
                        headers=_headers()).status_code == 404


def test_delete_category_cascade_with_confirm_count(seeded):
    cat_id = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "Работа"},
                         headers=_headers()).json()["category"]["id"]
    _save_fact("факт 1", cat_id)
    _save_fact("факт 2", cat_id)
    r = seeded.delete(f"/miniapp/api/v1/memory/categories/{cat_id}?confirm_count=2", headers=_headers())
    assert r.status_code == 200 and r.json()["deleted"] == 2
    # факты удалены вместе с категорией → list фактов этой категории теперь 404 (её нет)
    assert seeded.get(f"/miniapp/api/v1/memory/facts?category_id={cat_id}",
                      headers=_headers()).status_code == 404


def test_delete_category_confirm_mismatch_409(seeded):
    cat_id = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "Работа"},
                         headers=_headers()).json()["category"]["id"]
    _save_fact("факт", cat_id)
    r = seeded.delete(f"/miniapp/api/v1/memory/categories/{cat_id}?confirm_count=0", headers=_headers())
    assert r.status_code == 409 and r.json()["detail"] == "confirm_count_mismatch"


def test_delete_common_is_403(seeded):
    common_id = seeded.get("/miniapp/api/v1/memory/categories",
                           headers=_headers()).json()["categories"][0]["id"]
    r = seeded.delete(f"/miniapp/api/v1/memory/categories/{common_id}?confirm_count=0", headers=_headers())
    assert r.status_code == 403


def test_delete_category_negative_confirm_count_422(seeded):
    cat_id = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "Работа"},
                         headers=_headers()).json()["category"]["id"]
    r = seeded.delete(f"/miniapp/api/v1/memory/categories/{cat_id}?confirm_count=-1", headers=_headers())
    assert r.status_code == 422  # отрицательный confirm_count — заведомо невалиден


# --- facts: просмотр + правка (re-embed) + удаление --------------------------------

def test_list_facts_hides_embedding(seeded):
    cat_id = seeded.post("/miniapp/api/v1/memory/categories", json={"name": "Работа"},
                         headers=_headers()).json()["category"]["id"]
    _save_fact("мой факт", cat_id)
    facts = seeded.get(f"/miniapp/api/v1/memory/facts?category_id={cat_id}", headers=_headers()).json()["facts"]
    assert len(facts) == 1 and facts[0]["content"] == "мой факт"
    assert set(facts[0]) == {"id", "content", "tier", "source", "category_id", "created_at"}
    assert "embedding_json" not in facts[0] and "embedding_dim" not in facts[0]


# --- tenant isolation -----------------------------------------------------------

def test_cannot_access_other_users_category(seeded):
    foreign = _seed_other_user_category()
    # список фактов чужой категории → 404
    assert seeded.get(f"/miniapp/api/v1/memory/facts?category_id={foreign}",
                      headers=_headers()).status_code == 404
    # переименовать чужую → 404
    assert seeded.patch(f"/miniapp/api/v1/memory/categories/{foreign}", json={"name": "Х"},
                        headers=_headers()).status_code == 404
    # удалить чужую → 404
    assert seeded.delete(f"/miniapp/api/v1/memory/categories/{foreign}?confirm_count=0",
                         headers=_headers()).status_code == 404


# --- facts: правка (re-embed) + удаление ----------------------------------------

class _BoomEmbedder:
    """Эмбеддер, падающий при вызове — доказывает, что 404 отбивается ДО эмбеддинга."""

    def embed_document(self, _text):  # noqa: ANN001
        raise AssertionError("embedder must NOT be called for non-owned/nonexistent fact (404 before embed)")


def _seed_other_user_fact() -> str:
    """Второй тенант/юзер + его факт. Возвращает id чужого факта."""
    from sreda.db.repositories.memory import MemoryRepository
    from sreda.db.repositories.seed import SeedRepository
    from sreda.db.session import get_session_factory

    s = get_session_factory()()
    try:
        SeedRepository(s).ensure_tenant_bundle(
            tenant_id="tenant_other", tenant_name="O", workspace_id="wso", workspace_name="WO",
            user_id="user_other", telegram_account_id="999000", assistant_id="aso",
            assistant_name="O", eds_monitor_enabled=False)
        s.commit()
        m = MemoryRepository(s).save("tenant_other", "user_other", tier="core",
                                     content="чужой факт", embedding=[0.5, 0.6])
        s.commit()
        return m.id
    finally:
        s.close()


def test_edit_fact_reembeds_and_sets_user_direct(seeded, monkeypatch):
    monkeypatch.setattr("sreda.api.routes.miniapp.get_embeddings_client", lambda: FakeEmbeddingClient())
    fid = _save_fact("старый текст")
    r = seeded.patch(f"/miniapp/api/v1/memory/facts/{fid}", json={"content": "новый текст"}, headers=_headers())
    assert r.status_code == 200
    assert r.json()["fact"]["content"] == "новый текст"
    assert r.json()["fact"]["source"] == "user_direct"


def test_edit_fact_embedding_failure_503_not_applied(seeded, monkeypatch):
    class _Boom:
        def embed_document(self, _):
            raise RuntimeError("embedder down")

    monkeypatch.setattr("sreda.api.routes.miniapp.get_embeddings_client", lambda: _Boom())
    fid = _save_fact("исходный")
    r = seeded.patch(f"/miniapp/api/v1/memory/facts/{fid}", json={"content": "новый"}, headers=_headers())
    assert r.status_code == 503
    # правка НЕ применена — текст исходный (через list фактов Common)
    common_id = seeded.get("/miniapp/api/v1/memory/categories",
                           headers=_headers()).json()["categories"][0]["id"]
    facts = seeded.get(f"/miniapp/api/v1/memory/facts?category_id={common_id}", headers=_headers()).json()["facts"]
    assert any(f["content"] == "исходный" for f in facts)


def test_edit_fact_not_found_404_before_embed(seeded, monkeypatch):
    monkeypatch.setattr("sreda.api.routes.miniapp.get_embeddings_client", lambda: _BoomEmbedder())
    r = seeded.patch("/miniapp/api/v1/memory/facts/nope", json={"content": "x"}, headers=_headers())
    assert r.status_code == 404  # boom не вызван → префлайт отбил до эмбеддинга


def test_edit_fact_content_too_long_422(seeded, monkeypatch):
    monkeypatch.setattr("sreda.api.routes.miniapp.get_embeddings_client", lambda: FakeEmbeddingClient())
    fid = _save_fact("короткий")
    r = seeded.patch(f"/miniapp/api/v1/memory/facts/{fid}", json={"content": "а" * 5000}, headers=_headers())
    assert r.status_code == 422


def test_delete_fact_ok_and_404(seeded):
    fid = _save_fact("факт")
    assert seeded.delete(f"/miniapp/api/v1/memory/facts/{fid}", headers=_headers()).status_code == 200
    assert seeded.delete(f"/miniapp/api/v1/memory/facts/{fid}", headers=_headers()).status_code == 404


def test_cannot_edit_or_delete_foreign_fact(seeded, monkeypatch):
    """Изоляция по FACT-id: чужой факт нельзя править/удалить (404; правка — без вызова эмбеддера)."""
    monkeypatch.setattr("sreda.api.routes.miniapp.get_embeddings_client", lambda: _BoomEmbedder())
    foreign_fid = _seed_other_user_fact()
    assert seeded.patch(f"/miniapp/api/v1/memory/facts/{foreign_fid}", json={"content": "взлом"},
                        headers=_headers()).status_code == 404
    assert seeded.delete(f"/miniapp/api/v1/memory/facts/{foreign_fid}",
                         headers=_headers()).status_code == 404
