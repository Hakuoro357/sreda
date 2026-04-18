"""Unit tests for fetch_url — the URL→text tool for the chat LLM."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx

from sreda.services.web_search_tool import (
    _validate_url,
    build_fetch_url_tool,
)


def _invoke(tool, url: str) -> str:
    return tool.invoke({"url": url})


def _make_response(text: str, *, status: int = 200, content_type: str = "text/html; charset=utf-8", final_url: str | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = {"content-type": content_type}
    resp.text = text
    resp.url = final_url or "https://example.com/"
    if content_type.startswith("application/json"):
        resp.json.return_value = json.loads(text)
    return resp


def test_url_validation_rejects_localhost_and_private() -> None:
    for bad in [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.20.0.1/",
        "http://169.254.1.1/",
        "ftp://example.com/",
    ]:
        ok, reason = _validate_url(bad)
        assert not ok, f"expected {bad!r} to be rejected"
        assert reason


def test_url_validation_accepts_public_https() -> None:
    ok, _ = _validate_url("https://wttr.in/Moscow")
    assert ok
    ok, _ = _validate_url("http://api.example.com/v1/data?q=x")
    assert ok


def test_fetch_url_empty_input() -> None:
    tool = build_fetch_url_tool()
    assert _invoke(tool, "   ") == "error: empty url"


def test_fetch_url_blocked_host() -> None:
    tool = build_fetch_url_tool()
    result = _invoke(tool, "http://localhost/x")
    assert result.startswith("error:")


def test_fetch_url_html_extracts_article() -> None:
    html = """
    <html>
      <head><title>Sample page</title></head>
      <body>
        <article>
          <h1>Main heading</h1>
          <p>First paragraph with <a href="https://ex.com/a">link</a>.</p>
          <p>Second paragraph.</p>
        </article>
      </body>
    </html>
    """
    tool = build_fetch_url_tool()
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _make_response(html, content_type="text/html")

    with patch("httpx.Client", return_value=fake_client):
        result = _invoke(tool, "https://example.com/x")

    data = json.loads(result)
    assert data["extractor"] == "html"
    assert data["status"] == 200
    assert "Main heading" in data["text"] or "main heading" in data["text"].lower()
    # Untrusted banner is present.
    assert "Внешний контент" in data["text"]


def test_fetch_url_json_prettified() -> None:
    payload = {"location": "Moscow", "temp": -5}
    body = json.dumps(payload)
    tool = build_fetch_url_tool()
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _make_response(body, content_type="application/json")

    with patch("httpx.Client", return_value=fake_client):
        result = _invoke(tool, "https://api.ex.com/w")

    data = json.loads(result)
    assert data["extractor"] == "json"
    assert "Moscow" in data["text"]
    # Pretty-printed with indent.
    assert "  " in data["text"]


def test_fetch_url_plain_text_passthrough() -> None:
    body = "Сходня: ☁️ +12°C"
    tool = build_fetch_url_tool()
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _make_response(body, content_type="text/plain")

    with patch("httpx.Client", return_value=fake_client):
        result = _invoke(tool, "https://wttr.in/Сходня?format=3")

    data = json.loads(result)
    assert data["extractor"] == "raw"
    assert "Сходня" in data["text"]
    assert "+12°C" in data["text"]


def test_fetch_url_http_error_returns_error_string() -> None:
    tool = build_fetch_url_tool()
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _make_response("forbidden", status=403, content_type="text/plain")

    with patch("httpx.Client", return_value=fake_client):
        result = _invoke(tool, "https://example.com/x")

    assert result == "error: http 403"


def test_fetch_url_timeout_returns_error_string() -> None:
    tool = build_fetch_url_tool()
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.side_effect = httpx.TimeoutException("slow")

    with patch("httpx.Client", return_value=fake_client):
        result = _invoke(tool, "https://example.com/x")

    assert result.startswith("error: timeout")


def test_fetch_url_truncates_long_text() -> None:
    body = "x" * 10000  # plain text — raw extractor
    tool = build_fetch_url_tool()
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = _make_response(body, content_type="text/plain")

    with patch("httpx.Client", return_value=fake_client):
        result = _invoke(tool, "https://example.com/x")

    data = json.loads(result)
    assert data["truncated"] is True
    # banner (~90) + 3500 chars ≈ 3600
    assert data["length"] < 4000
