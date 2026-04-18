"""Unit tests for the web_search tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sreda.services.web_search_tool import build_web_search_tool


def _invoke(tool, **kwargs) -> str:
    return tool.invoke(kwargs)


def test_empty_query_returns_error() -> None:
    tool = build_web_search_tool()
    assert _invoke(tool, query="   ") == "error: empty query"


def test_formats_top_results_with_title_body_url() -> None:
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value = fake_ddgs
    fake_ddgs.__exit__.return_value = False
    fake_ddgs.text.return_value = [
        {"title": "T1", "body": "Body 1", "href": "https://ex.com/1"},
        {"title": "T2", "body": "Body 2", "href": "https://ex.com/2"},
        {"title": "T3", "body": "Body 3", "href": "https://ex.com/3"},
    ]

    tool = build_web_search_tool()
    with patch("duckduckgo_search.DDGS", return_value=fake_ddgs):
        result = _invoke(tool, query="Москва")

    assert "1. T1" in result
    assert "Body 1" in result
    assert "https://ex.com/1" in result
    assert "2. T2" in result
    assert "3. T3" in result
    fake_ddgs.text.assert_called_once()


def test_truncates_long_snippets() -> None:
    long_body = "x" * 500
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value = fake_ddgs
    fake_ddgs.__exit__.return_value = False
    fake_ddgs.text.return_value = [
        {"title": "T", "body": long_body, "href": "https://ex.com/1"},
    ]

    tool = build_web_search_tool()
    with patch("duckduckgo_search.DDGS", return_value=fake_ddgs):
        result = _invoke(tool, query="q")

    # 280 chars + ellipsis
    assert "x" * 280 in result
    assert "…" in result
    assert "x" * 500 not in result


def test_no_results_returns_no_results() -> None:
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value = fake_ddgs
    fake_ddgs.__exit__.return_value = False
    fake_ddgs.text.return_value = []

    tool = build_web_search_tool()
    with patch("duckduckgo_search.DDGS", return_value=fake_ddgs):
        result = _invoke(tool, query="nothing")

    assert result == "no results"


def test_network_failure_returns_error_string_not_exception() -> None:
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value = fake_ddgs
    fake_ddgs.__exit__.return_value = False
    fake_ddgs.text.side_effect = ConnectionError("down")

    tool = build_web_search_tool()
    with patch("duckduckgo_search.DDGS", return_value=fake_ddgs):
        result = _invoke(tool, query="q")

    assert result.startswith("error:")
    assert "ConnectionError" in result


def test_missing_package_returns_error() -> None:
    tool = build_web_search_tool()
    # Simulate the lazy import failing — we do this by removing the
    # module from the import cache and blocking the real import.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "duckduckgo_search":
            raise ImportError("mocked")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        result = _invoke(tool, query="q")

    assert result == "error: web_search not available"
