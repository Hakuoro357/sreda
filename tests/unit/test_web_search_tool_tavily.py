"""Unit-tests for `build_web_search_tool` Tavily-backed implementation.

Covers:
* Tavily happy path → формат «N. Title\\n<snippet>\\n<url>»
* Tavily 401/network → DDG fallback
* Quota exhausted → DDG fallback (не звоним в Tavily)
* DDG fallback hit инкрементирует `fallback_calls`
* Tavily success инкрементирует `tavily_calls`
* Empty query → "error: empty query"
* Tavily fail + DDG fail → "error: Достигнут лимит поиска"
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models.core import Tenant
from sreda.db.models.web_search import WebSearchUsage
from sreda.db.session import get_engine, get_session_factory
from sreda.services.web_search_tool import build_web_search_tool
from sreda.services.web_search_usage import (
    PER_USER_LIMIT,
    WebSearchUsageCounter,
)


def _bootstrap(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "ws_tool.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    session.add(Tenant(id="t1", name="Tenant One"))
    session.commit()
    return session


def _tavily_ok_response(results: list[dict]):
    """Mock httpx.post return for tavily."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"results": results}
    return resp


def _http_error_response(status_code: int):
    request = httpx.Request("POST", "https://api.tavily.com/search")
    response = httpx.Response(status_code, request=request)
    err = httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)
    resp = MagicMock()
    resp.raise_for_status.side_effect = err
    resp.response = response
    return resp


# --------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------


def test_tavily_happy_path_increments_counter(monkeypatch, tmp_path):
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
        )
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post:
            mock_post.return_value = _tavily_ok_response([
                {"title": "Title A", "content": "Snippet A", "url": "https://a.example"},
                {"title": "Title B", "content": "Snippet B", "url": "https://b.example"},
            ])
            result = tool.invoke({"query": "test query"})

        # Format check
        assert "1. Title A" in result
        assert "Snippet A" in result
        assert "https://a.example" in result
        assert "2. Title B" in result

        # Counter incremented
        row = session.query(WebSearchUsage).one()
        assert row.tavily_calls == 1
        assert row.fallback_calls == 0
    finally:
        session.close()


def test_tavily_no_results_returns_no_results_string(monkeypatch, tmp_path):
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
        )
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post:
            mock_post.return_value = _tavily_ok_response([])
            result = tool.invoke({"query": "obscure"})

        assert result == "no results"
        # Counter всё равно инкрементируется (Tavily запрос-то отправили)
        row = session.query(WebSearchUsage).one()
        assert row.tavily_calls == 1
    finally:
        session.close()


# --------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------


def test_empty_query_short_circuits(monkeypatch, tmp_path):
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
        )
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post:
            result = tool.invoke({"query": "  "})
        # Tavily НЕ вызывался
        mock_post.assert_not_called()
        assert result == "error: empty query"
    finally:
        session.close()


# --------------------------------------------------------------------
# Quota exhaustion → DDG fallback
# --------------------------------------------------------------------


def test_per_user_quota_exhausted_falls_back_to_ddg(monkeypatch, tmp_path):
    """Per-user limit достигнут → tool НЕ зовёт tavily, идёт в DDG.

    Mock-уем `_call_ddg_fallback` напрямую (а не саму ddgs-библиотеку),
    чтобы тесты работали даже без установленного duckduckgo_search."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        # Bump per-user счётчик до лимита
        counter = WebSearchUsageCounter(session)
        for _ in range(PER_USER_LIMIT):
            counter.record_tavily(tenant_id="t1", user_id="u1")

        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
        )

        ddg_results = [
            ("DDG Title", "DDG snippet", "https://ddg.example"),
        ]

        with patch("sreda.services.web_search_tool.httpx.post") as mock_post, \
             patch(
                 "sreda.services.web_search_tool._call_ddg_fallback",
                 return_value=ddg_results,
             ):
            result = tool.invoke({"query": "test"})

        # Tavily НЕ вызывался (квота исчерпана заранее)
        mock_post.assert_not_called()
        assert "DDG Title" in result
        assert "DDG snippet" in result

        # fallback_calls инкрементирован
        row = (
            session.query(WebSearchUsage)
            .filter_by(tenant_id="t1", user_id="u1")
            .one()
        )
        assert row.tavily_calls == PER_USER_LIMIT  # уже было
        assert row.fallback_calls == 1
    finally:
        session.close()


def test_tavily_http_error_falls_back_to_ddg(monkeypatch, tmp_path):
    """Tavily вернул 401/500 → fall back to DDG."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
        )

        with patch("sreda.services.web_search_tool.httpx.post") as mock_post, \
             patch(
                 "sreda.services.web_search_tool._call_ddg_fallback",
                 return_value=[("DDG", "fb", "https://x")],
             ):
            mock_post.return_value = _http_error_response(401)
            result = tool.invoke({"query": "test"})

        assert "DDG" in result
        # Tavily вызван 1 раз, but failed
        assert mock_post.call_count == 1
        # tavily_calls НЕ инкрементирован (только при success)
        # fallback_calls инкрементирован
        row = session.query(WebSearchUsage).one()
        assert row.tavily_calls == 0
        assert row.fallback_calls == 1
    finally:
        session.close()


# --------------------------------------------------------------------
# Both fail → quota exhausted message
# --------------------------------------------------------------------


def test_tavily_fail_plus_ddg_fail_returns_quota_msg(monkeypatch, tmp_path):
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
        )

        with patch("sreda.services.web_search_tool.httpx.post") as mock_post, \
             patch(
                 "sreda.services.web_search_tool._call_ddg_fallback",
                 return_value=None,
             ):
            mock_post.return_value = _http_error_response(500)
            result = tool.invoke({"query": "test"})

        assert result == "error: Достигнут лимит поиска"
        # Никакие counter'ы не инкрементируем — оба fail
        row = session.query(WebSearchUsage).first()
        assert row is None or (row.tavily_calls == 0 and row.fallback_calls == 0)
    finally:
        session.close()


# --------------------------------------------------------------------
# Without session/tenant — quota check skipped
# --------------------------------------------------------------------


def test_no_session_skips_quota_check(monkeypatch, tmp_path):
    """tool вызван без session/tenant_id (legacy / scripts) — quota
    не отслеживается, но Tavily всё равно работает."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool()  # without session/tenant
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post:
            mock_post.return_value = _tavily_ok_response([
                {"title": "A", "content": "B", "url": "https://c"},
            ])
            result = tool.invoke({"query": "test"})

        assert "1. A" in result
        # Никаких counter rows не создано
        rows = session.query(WebSearchUsage).all()
        assert rows == []
    finally:
        session.close()
