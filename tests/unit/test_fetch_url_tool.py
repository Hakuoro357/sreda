"""Unit tests for fetch_url — оркестрация tool'а (#244: SSRF-egress + квота).

URL-валидация (схемы/IP/numeric) — в test_ssrf_guard_244.py; fetch-движок (редиректы/байт-лимит/
decompress/preflight) — в test_fetch_url_client_244.py. Здесь — ПОТОК замыкания:
early-validate → per-turn → quota-context → pre-flight → reserve(no-refund) → fetch → extract.
"""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — регистрирует FetchUrlUsage
from sreda.services import web_search_tool
from sreda.services.fetch_url_client import FetchResult
from sreda.services.web_search_tool import build_fetch_url_tool


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    return sessionmaker(bind=engine)()


def _tool(session, *, is_free=False, per_day_cap=None, per_turn_cap=None,
          proxy="socks5://127.0.0.1:1081"):
    return build_fetch_url_tool(
        session=session, tenant_id="ten", user_id="usr",
        is_free=is_free, per_day_cap=per_day_cap,
        per_turn_cap=per_turn_cap, proxy_url=proxy,
    )


def _invoke(tool, url: str) -> str:
    return tool.invoke({"url": url})


def _ok_preflight(monkeypatch):
    monkeypatch.setattr(web_search_tool, "preflight_egress", lambda p: (True, ""))


def _stub_fetch(monkeypatch, result):
    monkeypatch.setattr(web_search_tool, "fetch_via_filtered_egress", lambda u, p: result)


# ── базовый поток / fail-closed ──────────────────────────────────────────
def test_empty_input(session):
    assert _invoke(_tool(session), "   ") == "error: empty url"


def test_early_reject_private_before_quota(session):
    # validate_fetch_url рубит приватный литерал ДО квоты/сети
    assert _invoke(_tool(session), "http://127.0.0.1/").startswith("error:")


def test_no_context_fail_closed():
    # нет session/tenant/user → error:fetch_quota_unavailable ДО сети
    tool = build_fetch_url_tool(proxy_url="socks5://127.0.0.1:1081")
    assert _invoke(tool, "https://example.com/") == "error:fetch_quota_unavailable"


def test_empty_proxy_fail_closed(session):
    # proxy пуст → pre-flight fail-closed (без мока pre-flight)
    tool = _tool(session, proxy="")
    assert _invoke(tool, "https://example.com/") == "error:fetch_egress_unavailable"


# ── extract (мок pre-flight + движок) ────────────────────────────────────
def test_html_extracts_article(session, monkeypatch):
    _ok_preflight(monkeypatch)
    html = ("<html><head><title>Sample page</title></head><body><article>"
            "<h1>Main heading</h1><p>First paragraph with "
            '<a href="https://ex.com/a">link</a>.</p><p>Second paragraph.</p>'
            "</article></body></html>")
    _stub_fetch(monkeypatch, FetchResult(200, "https://example.com/x", "text/html", html))
    data = json.loads(_invoke(_tool(session), "https://example.com/x"))
    assert data["extractor"] == "html"
    assert data["status"] == 200
    assert "heading" in data["text"].lower()
    assert "Внешний контент" in data["text"]  # untrusted-баннер


def test_json_prettified(session, monkeypatch):
    _ok_preflight(monkeypatch)
    body = json.dumps({"location": "Moscow", "temp": -5})
    _stub_fetch(monkeypatch, FetchResult(200, "https://api.ex.com/w", "application/json", body))
    data = json.loads(_invoke(_tool(session), "https://api.ex.com/w"))
    assert data["extractor"] == "json"
    assert "Moscow" in data["text"]
    assert "  " in data["text"]  # indent


def test_plain_text_passthrough(session, monkeypatch):
    _ok_preflight(monkeypatch)
    body = "Сходня: ☁️ +12°C"
    _stub_fetch(monkeypatch, FetchResult(200, "https://wttr.in/x", "text/plain", body))
    data = json.loads(_invoke(_tool(session), "https://wttr.in/x"))
    assert data["extractor"] == "raw"
    assert "Сходня" in data["text"] and "+12°C" in data["text"]


def test_http_error_returns_error_string(session, monkeypatch):
    _ok_preflight(monkeypatch)
    _stub_fetch(monkeypatch, FetchResult(403, "https://example.com/x", "text/plain", "forbidden"))
    assert _invoke(_tool(session), "https://example.com/x") == "error: http 403"


def test_timeout_returns_error_string(session, monkeypatch):
    _ok_preflight(monkeypatch)

    def _raise(u, p):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(web_search_tool, "fetch_via_filtered_egress", _raise)
    assert _invoke(_tool(session), "https://example.com/x").startswith("error: timeout")


def test_truncates_long_text(session, monkeypatch):
    _ok_preflight(monkeypatch)
    _stub_fetch(monkeypatch, FetchResult(200, "https://example.com/x", "text/plain", "x" * 20000))
    data = json.loads(_invoke(_tool(session), "https://example.com/x"))
    assert data["truncated"] is True
    assert 12000 < data["length"] < 13000


def test_medium_json_not_truncated(session, monkeypatch):
    _ok_preflight(monkeypatch)
    body = json.dumps({"hours": [{"t": i, "temp": i} for i in range(200)]})
    assert 3500 < len(body) < 12000
    _stub_fetch(monkeypatch, FetchResult(200, "https://api.ex.com/w", "application/json", body))
    data = json.loads(_invoke(_tool(session), "https://api.ex.com/w"))
    assert data["truncated"] is False
    assert '"t": 199' in data["text"]


# ── квота (no-refund) + per-turn ─────────────────────────────────────────
def test_quota_exhausted_after_cap(session, monkeypatch):
    _ok_preflight(monkeypatch)
    _stub_fetch(monkeypatch, FetchResult(200, "https://e.com/", "text/plain", "ok"))
    tool = _tool(session, is_free=True, per_day_cap=1, per_turn_cap=0)
    assert _invoke(tool, "https://e.com/").startswith("{")          # 1-й ок
    assert _invoke(tool, "https://e.com/").startswith("error:fetch_quota_exhausted")  # 2-й исчерпан


def test_per_turn_cap(session, monkeypatch):
    _ok_preflight(monkeypatch)
    _stub_fetch(monkeypatch, FetchResult(200, "https://e.com/", "text/plain", "ok"))
    tool = _tool(session, is_free=False, per_turn_cap=1)  # cap=None (без дневного) → проверяем именно per-turn
    assert _invoke(tool, "https://e.com/").startswith("{")          # 1-й ок
    assert _invoke(tool, "https://e.com/").startswith("error:fetch_turn_limit")  # 2-й — шторм-стоп


def test_paid_no_daily_limit(session, monkeypatch):
    _ok_preflight(monkeypatch)
    _stub_fetch(monkeypatch, FetchResult(200, "https://e.com/", "text/plain", "ok"))
    tool = _tool(session, is_free=False, per_day_cap=1, per_turn_cap=0)  # is_free=False → cap игнорируется
    for _ in range(4):
        assert _invoke(tool, "https://e.com/").startswith("{")  # без дневного лимита
