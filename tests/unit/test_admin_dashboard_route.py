"""Route-level tests for the overview dashboard (#292, R1 fixes).

The central Phase A contract (high R1 MINOR → named test): ``GET /admin``
renders from the STORED snapshot — with every recompute/network helper
broken, the page must still return 200. Plus: normalizer coercion,
per-block fail-soft, protected upsert.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.admin import host_metrics as hm
from sreda.admin import overview_snapshot as ov
from sreda.admin import routes as admin_routes
from sreda.admin.auth import require_admin_token
from sreda.db.base import Base


@pytest.fixture()
def session_factory():
    # StaticPool: одно соединение на все потоки — иначе TestClient
    # (другой поток) получает НОВУЮ пустую in-memory БД.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def client(session_factory, monkeypatch):
    app = FastAPI()
    app.include_router(admin_routes.router)

    def _session_override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[require_admin_token] = lambda: "T"
    app.dependency_overrides[admin_routes._get_session] = _session_override
    # host-метрики без subprocess'ов в тестах
    monkeypatch.setattr(hm, "_systemctl", lambda args: None)
    monkeypatch.setattr(hm, "_read_text", lambda path: None)
    return TestClient(app)


def _boom(*a, **k):
    raise RuntimeError("must not be called on GET /admin")


def test_dashboard_renders_from_snapshot_without_recompute(
    client, session_factory, monkeypatch
):
    # снапшот подготовлен фоном (эмулируем)
    s = session_factory()
    ov.store_snapshot(s, ov.KEY_OVERVIEW, {
        "llm_24h": {"calls": 7, "errors": 1, "error_rate_pct": 14.3, "slow": 0},
        "balances": [{"key": "openrouter", "label": "OpenRouter",
                      "status": "ok", "headline": "$12.40", "details": ""}],
    })
    s.close()
    # ВСЕ пересчётные/сетевые хелперы уронены — страница обязана жить
    monkeypatch.setattr(ov, "compute_overview", _boom)
    monkeypatch.setattr(ov, "refresh_overview", _boom)
    monkeypatch.setattr(
        "sreda.services.provider_balances.fetch_balances", _boom)
    monkeypatch.setattr(
        "sreda.admin.queries.get_cost_volume_summary", _boom)
    resp = client.get("/admin/", params={"token": "T"})
    assert resp.status_code == 200
    assert "OpenRouter" in resp.text
    assert "снапшот обновлён" in resp.text


def test_dashboard_renders_when_no_snapshot(client):
    resp = client.get("/admin/", params={"token": "T"})
    assert resp.status_code == 200
    assert "снапшот ещё не собран" in resp.text


def test_dashboard_survives_garbage_snapshot(client, session_factory):
    # битый/устаревший payload (не та схема, мусорные типы) → 200, не 500
    s = session_factory()
    ov.store_snapshot(s, ov.KEY_OVERVIEW, {
        "llm_24h": {"calls": "мусор", "errors": None},
        "cost": {"day": {"priced_subtotal_usd": "NaN-строка", "rows": "не список"}},
        "slow_recent": [{"latency_ms": "медленно"}, "не dict"],
        "balances": "тоже не список",
        "top_tenants": {"by_spend": [{"est_usd": "x"}]},
    })
    s.close()
    resp = client.get("/admin/", params={"token": "T"})
    assert resp.status_code == 200


def test_refresh_endpoint_delegates_and_redirects(client, monkeypatch):
    called = {}
    monkeypatch.setattr(
        ov, "refresh_overview",
        lambda sf, st: called.setdefault("yes", True) or True,
    )
    resp = client.post(
        "/admin/refresh-snapshot", params={"token": "T"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert called.get("yes") is True
    assert "refresh=err" not in resp.headers["location"]


def test_normalize_overview_coerces_garbage():
    norm = ov.normalize_overview({
        "llm_24h": {"calls": "x", "errors": 2, "error_rate_pct": "y"},
        "cost": {"day": {"priced_subtotal_usd": "z", "rows": [
            {"provider_key": 5, "calls": "7", "priced": 1, "est_usd": "bad"},
            "не dict",
        ]}},
        "errors_recent": [{"at": 123}, "мусор"],
        "slow_turns": {"count_24h": "x", "recent": [{"total_ms": "y"}, "не dict"]},
        "users": {"total": "x", "new_today": 2, "new_7d": None},
        "purchases": {"paid_tenants": True, "orders_7d": 1, "sum_rub_7d": "z",
                      "orders_30d": None, "sum_rub_30d": 800},
        "llm_chain": {"primary": 9},
    })
    assert norm["llm_24h"] == {"calls": 0, "errors": 2, "error_rate_pct": 0.0}
    assert norm["slow_turns"]["count_24h"] == 0
    assert norm["slow_turns"]["recent"][0]["total_ms"] == 0
    assert norm["users"] == {"total": 0, "new_today": 2, "new_7d": 0}
    assert norm["purchases"]["paid_tenants"] == 0  # bool не число
    assert norm["purchases"]["sum_rub_30d"] == 800
    day = norm["cost"]["day"]
    assert day["priced_subtotal_usd"] == 0.0
    assert day["rows"][0]["provider_key"] == ""  # coerced, не 500
    assert day["rows"][0]["est_usd"] is None      # не выдумываем деньги
    # R3: bool — подкласс int; est_usd=True НЕ должен стать $0.0/priced
    norm_b = ov.normalize_overview({
        "cost": {"day": {"rows": [{"priced": True, "est_usd": True}]}}})
    row_b = norm_b["cost"]["day"]["rows"][0]
    assert row_b["est_usd"] is None and row_b["priced"] is False
    assert len(norm["errors_recent"]) == 1 and norm["errors_recent"][0]["at"] == ""
    assert norm["llm_chain"]["primary"] == ""


def test_normalize_overview_keeps_valid_values():
    payload = {
        "llm_24h": {"calls": 4, "errors": 2, "error_rate_pct": 50.0, "slow": 1},
        "cost": {"day": {
            "priced_subtotal_usd": 0.61, "upper_subtotal_usd": 0.7,
            "calls": 10, "coverage_calls_pct": 93,
            "unpriced_calls": 1, "unpriced_tokens": 500,
            "rows": [{"provider_key": "p", "model": "m", "calls": 3,
                      "prompt_tokens": 1, "completion_tokens": 2,
                      "priced": True, "est_usd": 0.5, "upper_usd": 0.6}],
            "unpriced_rows": [],
        }},
    }
    norm = ov.normalize_overview(payload)
    assert norm["llm_24h"]["error_rate_pct"] == 50.0
    assert norm["cost"]["day"]["rows"][0]["est_usd"] == 0.5
    assert norm["cost"]["day"]["coverage_calls_pct"] == 93


def test_db_error_in_block_does_not_prevent_store(session_factory, monkeypatch):
    # R2 (high+medium MAJOR): сбойный SQL в блоке НЕ должен помешать
    # сохранению частичного снапшота (rollback в _safe).
    import sqlalchemy.exc

    def _sql_boom(*a, **k):
        raise sqlalchemy.exc.OperationalError("SELECT boom", {}, Exception("x"))

    monkeypatch.setattr(
        "sreda.admin.queries.get_cost_volume_summary", _sql_boom)
    monkeypatch.setattr(
        ov, "_balances_block", lambda settings: [])
    ok = ov.refresh_overview(
        session_factory,
        SimpleNamespace(chat_provider="p", chat_fallback_provider=""),
    )
    assert ok is True  # частичный снапшот СОХРАНЁН
    s = session_factory()
    loaded, at = ov.load_snapshot(s, ov.KEY_OVERVIEW)
    s.close()
    assert at is not None
    assert loaded["cost"] == {}          # сбойный блок деградировал
    assert "llm_24h" in loaded            # остальные собрались


def test_garbage_priced_row_renders_200(client, session_factory):
    # R2 (high MAJOR): priced=True без числа НЕ должен дать 500 —
    # нормализатор обязан сбросить priced.
    s = session_factory()
    ov.store_snapshot(s, ov.KEY_OVERVIEW, {
        "cost": {"day": {
            "priced_subtotal_usd": 1.0, "upper_subtotal_usd": 1.0,
            "calls": 1, "coverage_calls_pct": 50,
            "unpriced_calls": 0, "unpriced_tokens": 0,
            "rows": [{"provider_key": "p", "model": "m", "calls": 1,
                      "prompt_tokens": 1, "completion_tokens": 1,
                      "priced": True, "est_usd": None}],
            "unpriced_rows": [],
        }},
    })
    s.close()
    resp = client.get("/admin/", params={"token": "T"})
    assert resp.status_code == 200


def test_compute_overview_per_block_failsoft(session_factory, monkeypatch):
    # уроненный блок → пустой дефолт, остальные блоки живут
    monkeypatch.setattr(ov, "_llm_24h_block", _boom)
    monkeypatch.setattr(
        ov, "_balances_block",
        lambda settings: [{"key": "k", "label": "L", "status": "ok",
                           "headline": "h", "details": ""}],
    )
    s = session_factory()
    payload = ov.compute_overview(
        s, SimpleNamespace(chat_provider="p", chat_fallback_provider=""))
    s.close()
    assert payload["llm_24h"] == {}          # деградировал
    assert payload["balances"][0]["key"] == "k"  # остальное собралось
    assert "cost" in payload
