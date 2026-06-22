"""Unit-tests for `build_web_search_tool` Tavily-backed implementation.

#200 Фаза 1 — тир-aware политика веб-поиска (веб-поиск доступен ВСЕМ;
free=5/мес жёстко, grandfathered/платные без per-user лимита; global 950
атомарно). Покрывает:
* Tavily happy path → формат «N. Title\\n<snippet>\\n<url>», слот считается
* пустой результат → «no results», слот СЧИТАЕТСЯ
* Tavily 401/None (provider-fail) → release слота + транзиентная ошибка (НЕ DDG)
* free 6-й вызов → машинный статус апсейла (Tavily НЕ зван)
* grandfathered (per_user_cap=None) → 31-й проходит
* missing context + is_free → fail-closed (quota exhausted, не Tavily)
* global≥950 + free → транзиентная (без Tavily/DDG)
* Empty query → "error: empty query"
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models.core import Tenant
from sreda.db.models.web_search import WebSearchUsage
from sreda.db.session import get_engine, get_session_factory
from sreda.services.web_search_tool import (
    _FREE_QUOTA_EXHAUSTED_MSG,
    _WEB_SEARCH_UNAVAILABLE_MSG,
    build_web_search_tool,
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

        # Counter incremented atomically (один слот занят).
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
        # Пустой результат = занятый слот (Tavily запрос-то отправили).
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
# Free per-user exhaustion → апсейл-статус (НЕ DDG)
# --------------------------------------------------------------------


def test_web_search_free_hard_stop_after_5(monkeypatch, tmp_path):
    """free (per_user_cap=5): ровно 5 Tavily-вызовов проходят; 6-й →
    машинный статус апсейла, Tavily НЕ зван (call_count == 5)."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
            per_user_cap=5, is_free=True,
        )
        with patch("sreda.services.web_search_tool._call_tavily") as mock_tavily:
            mock_tavily.return_value = [("T", "B", "https://x")]
            results = [tool.invoke({"query": f"q{i}"}) for i in range(6)]

        # Первые 5 — успех, 6-й — апсейл-статус.
        assert all("1. T" in r for r in results[:5])
        assert results[5] == _FREE_QUOTA_EXHAUSTED_MSG
        # Tavily вызван ровно 5 раз — 6-й НЕ дошёл.
        assert mock_tavily.call_count == 5

        row = session.query(WebSearchUsage).one()
        assert row.tavily_calls == 5
        assert row.fallback_calls == 0
    finally:
        session.close()


def test_web_search_grandfathered_no_user_cap(monkeypatch, tmp_path):
    """grandfathered/платные (per_user_cap=None): 31-й вызов проходит
    (нет per-user стопа)."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
            per_user_cap=None, is_free=False,
        )
        with patch("sreda.services.web_search_tool._call_tavily") as mock_tavily:
            mock_tavily.return_value = [("T", "B", "https://x")]
            for i in range(31):
                result = tool.invoke({"query": f"q{i}"})

        assert "1. T" in result  # 31-й прошёл
        assert mock_tavily.call_count == 31
        row = session.query(WebSearchUsage).one()
        assert row.tavily_calls == 31
    finally:
        session.close()


# --------------------------------------------------------------------
# Provider failure → release slot
# --------------------------------------------------------------------


def test_web_search_provider_failure_releases_slot(monkeypatch, tmp_path):
    """Tavily None (5xx/timeout/auth) → release слота + транзиентная
    ошибка (НЕ DDG, НЕ апсейл); счётчик не растёт."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
            per_user_cap=5, is_free=True,
        )
        ddg = MagicMock(return_value=[("DDG", "fb", "https://x")])
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post, \
             patch("sreda.services.web_search_tool._call_ddg_fallback", ddg):
            mock_post.return_value = _http_error_response(500)
            result = tool.invoke({"query": "test"})

        # Транзиентная ошибка, НЕ DDG.
        assert result == _WEB_SEARCH_UNAVAILABLE_MSG
        ddg.assert_not_called()
        # Слот зарезервировали → потом release → счётчик 0.
        row = session.query(WebSearchUsage).one()
        assert row.tavily_calls == 0
    finally:
        session.close()


def test_web_search_empty_results_counts(monkeypatch, tmp_path):
    """Пустой результат Tavily = занятый слот (НЕ release)."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
            per_user_cap=5, is_free=True,
        )
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post:
            mock_post.return_value = _tavily_ok_response([])
            result = tool.invoke({"query": "obscure"})

        assert result == "no results"
        row = session.query(WebSearchUsage).one()
        assert row.tavily_calls == 1  # СЧИТАЕТСЯ
    finally:
        session.close()


# --------------------------------------------------------------------
# Fail-closed: missing context
# --------------------------------------------------------------------


def test_web_search_missing_context_fail_closed(monkeypatch, tmp_path):
    """is_free + НЕТ полного контекста (session) → апсейл-статус, НЕ
    безлимитный Tavily."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        # Контекст неполный: tenant/user без session.
        tool = build_web_search_tool(
            session=None, tenant_id="t1", user_id="u1",
            per_user_cap=5, is_free=True,
        )
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post:
            result = tool.invoke({"query": "test"})

        assert result == _FREE_QUOTA_EXHAUSTED_MSG
        mock_post.assert_not_called()
    finally:
        session.close()


def test_web_search_missing_context_non_free_uses_ddg(monkeypatch, tmp_path):
    """non-free без полного контекста → DDG (не апсейл, не Tavily)."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        tool = build_web_search_tool(
            session=None, tenant_id=None, user_id=None,
            per_user_cap=None, is_free=False,
        )
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post, \
             patch(
                 "sreda.services.web_search_tool._call_ddg_fallback",
                 return_value=[("DDG", "fb", "https://x")],
             ):
            result = tool.invoke({"query": "test"})

        assert "DDG" in result
        mock_post.assert_not_called()
    finally:
        session.close()


# --------------------------------------------------------------------
# Global 950 → free транзиентная (без Tavily); non-free → DDG
# --------------------------------------------------------------------


def test_web_search_global_950_blocks_free_no_tavily(monkeypatch, tmp_path):
    """global ≥ 950 + free → транзиентная недоступность; Tavily НЕ зван,
    DDG НЕ зван, слот не считается."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        # Накачиваем глобальный счётчик до 950 через прямые строки
        # (разные юзеры по 30 — но per-user тут неважно, важна SUM).
        from sreda.services.web_search_usage import (
            QuotaDecision,
            try_consume_tavily,
        )
        engine = session.get_bind()
        for u in range(950 // 30 + 1):
            cap = None
            for _ in range(min(30, 950 - u * 30)):
                dec = try_consume_tavily(engine, "t1", f"gu{u}", cap)
                assert dec is QuotaDecision.ALLOW
        session.expire_all()

        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="ufresh",
            per_user_cap=5, is_free=True,
        )
        ddg = MagicMock(return_value=[("DDG", "fb", "https://x")])
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post, \
             patch("sreda.services.web_search_tool._call_ddg_fallback", ddg):
            result = tool.invoke({"query": "test"})

        assert result == _WEB_SEARCH_UNAVAILABLE_MSG
        mock_post.assert_not_called()
        ddg.assert_not_called()
    finally:
        session.close()


def test_web_search_global_950_non_free_uses_ddg(monkeypatch, tmp_path):
    """global ≥ 950 + non-free → DDG fallback (Tavily не зван)."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        from sreda.services.web_search_usage import (
            QuotaDecision,
            try_consume_tavily,
        )
        engine = session.get_bind()
        for u in range(950 // 30 + 1):
            for _ in range(min(30, 950 - u * 30)):
                dec = try_consume_tavily(engine, "t1", f"gu{u}", None)
                assert dec is QuotaDecision.ALLOW
        session.expire_all()

        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="ufresh",
            per_user_cap=None, is_free=False,
        )
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post, \
             patch(
                 "sreda.services.web_search_tool._call_ddg_fallback",
                 return_value=[("DDG", "fb", "https://x")],
             ):
            result = tool.invoke({"query": "test"})

        assert "DDG" in result
        mock_post.assert_not_called()
    finally:
        session.close()


# --------------------------------------------------------------------
# No api_key → DDG (quota не применяется)
# --------------------------------------------------------------------


def test_no_api_key_uses_ddg(monkeypatch, tmp_path):
    """Без TAVILY_API_KEY tool идёт в DDG как раньше."""
    session = _bootstrap(monkeypatch, tmp_path)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        tool = build_web_search_tool(
            session=session, tenant_id="t1", user_id="u1",
            per_user_cap=5, is_free=True,
        )
        with patch("sreda.services.web_search_tool.httpx.post") as mock_post, \
             patch(
                 "sreda.services.web_search_tool._call_ddg_fallback",
                 return_value=[("DDG", "fb", "https://x")],
             ):
            result = tool.invoke({"query": "test"})

        assert "DDG" in result
        mock_post.assert_not_called()
        # fallback_calls инкрементирован.
        row = session.query(WebSearchUsage).one()
        assert row.fallback_calls == 1
        assert row.tavily_calls == 0
    finally:
        session.close()
