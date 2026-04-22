"""Tests for provider-balance fetchers: graceful degradation (one
provider's outage must not hide the others), correct parsing of the
OpenRouter + rate-limit-headers shapes, and respect for the cache.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sreda.services import provider_balances as pb


class FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._body = json_body or {}
        self.headers = httpx.Headers(headers or {})
        self.text = str(self._body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self,  # type: ignore[arg-type]
            )


class FakeClient:
    """Captures URL per GET and returns scripted responses keyed by
    substring. Route ``/key`` → OpenRouter body; route ``/models`` →
    provider-specific rate-limit headers."""

    responses: dict[str, FakeResponse]

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        for substring, resp in self.responses.items():
            if substring in url:
                return resp
        raise AssertionError(f"no stub for URL {url}")


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    pb.invalidate_cache()
    # Strip any SREDA_* env leakage so Settings picks up constructor
    # kwargs cleanly instead of inheriting dev-shell secrets.
    import os
    for var in list(os.environ):
        if var.startswith("SREDA_"):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture
def _patch_httpx(monkeypatch: pytest.MonkeyPatch):
    def _apply(responses: dict[str, FakeResponse]):
        FakeClient.responses = responses
        monkeypatch.setattr(pb.httpx, "Client", FakeClient)

    return _apply


def _settings(**overrides: Any):
    """Bypass pydantic validation so tests can set ``*_api_key`` fields
    that normally only accept values via SREDA_* env aliases. Attribute
    assignment works because pydantic v2 models permit it post-construct
    for instance-level overrides."""
    from sreda.config.settings import Settings

    s = Settings()
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------


def test_openrouter_reports_spend_and_remaining(_patch_httpx) -> None:
    _patch_httpx({
        "/key": FakeResponse(200, json_body={"data": {
            "usage": 0.153,
            "limit": 10.0,
            "limit_remaining": 9.847,
            "usage_daily": 0.049,
        }}),
        "/models": FakeResponse(200),
    })
    s = _settings(openrouter_api_key="or-k")
    rows = pb.fetch_balances(s)
    row = next(r for r in rows if r.key == "openrouter")
    assert row.status == "ok"
    assert "9.8470" in row.headline or "9.847" in row.headline
    assert "10.0000" in row.headline or "10.0" in row.headline
    assert "0.0490" in row.details or "0.049" in row.details


def test_openrouter_missing_key_labels_not_configured(_patch_httpx) -> None:
    _patch_httpx({"/models": FakeResponse(200)})
    s = _settings()
    rows = pb.fetch_balances(s)
    row = next(r for r in rows if r.key == "openrouter")
    assert row.status == "not_configured"


def test_openrouter_http_error_doesnt_hide_others(_patch_httpx) -> None:
    _patch_httpx({
        "/key": FakeResponse(500),
        "/models": FakeResponse(200, headers={
            "x-ratelimit-remaining-requests": "42",
            "x-ratelimit-limit-requests": "1000",
        }),
    })
    s = _settings(openrouter_api_key="or-k", groq_api_key="gk")
    rows = pb.fetch_balances(s)
    or_row = next(r for r in rows if r.key == "openrouter")
    groq_row = next(r for r in rows if r.key == "groq")
    assert or_row.status == "error"
    assert groq_row.status == "ok", "Groq must still be queried when OR fails"


def test_openrouter_unlimited_account_renders_spend_only(_patch_httpx) -> None:
    _patch_httpx({
        "/key": FakeResponse(200, json_body={"data": {
            "usage": 1.23,
            "limit": None,
            "usage_daily": 0.0,
        }}),
        "/models": FakeResponse(200),
    })
    s = _settings(openrouter_api_key="or-k")
    rows = pb.fetch_balances(s)
    row = next(r for r in rows if r.key == "openrouter")
    assert row.status == "ok"
    assert "лимит не задан" in row.headline
    assert "1.23" in row.headline


# ---------------------------------------------------------------------------
# Rate-limited providers (Groq, MiMo)
# ---------------------------------------------------------------------------


def test_groq_reads_rate_limit_headers(_patch_httpx) -> None:
    _patch_httpx({
        "/models": FakeResponse(200, headers={
            "x-ratelimit-remaining-requests": "1998",
            "x-ratelimit-limit-requests": "2000",
            "x-ratelimit-remaining-tokens": "99000",
            "x-ratelimit-limit-tokens": "100000",
        }),
    })
    s = _settings(groq_api_key="g-k")
    rows = pb.fetch_balances(s)
    row = next(r for r in rows if r.key == "groq")
    assert row.status == "ok"
    assert "1998/2000" in row.details
    assert "99000/100000" in row.details


def test_mimo_without_rate_limit_headers_flagged_not_supported(_patch_httpx) -> None:
    """MiMo endpoints don't publish ratelimit headers. UI should show
    a neutral 'n/a' rather than 'error'."""
    _patch_httpx({"/models": FakeResponse(200)})  # no headers
    s = _settings(mimo_api_key="m-k")
    rows = pb.fetch_balances(s)
    row = next(r for r in rows if r.key == "mimo")
    assert row.status == "not_supported"


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_second_http_call(_patch_httpx, monkeypatch) -> None:
    calls = {"n": 0}

    class CountingClient(FakeClient):
        def get(self, url: str, **kwargs: Any) -> FakeResponse:
            calls["n"] += 1
            return super().get(url, **kwargs)

    FakeClient.responses = {
        "/key": FakeResponse(200, json_body={"data": {
            "usage": 0.1, "limit": 10.0, "usage_daily": 0.0,
        }}),
        "/models": FakeResponse(200),
    }
    monkeypatch.setattr(pb.httpx, "Client", CountingClient)

    s = _settings(openrouter_api_key="or-k", groq_api_key="g-k")
    pb.fetch_balances(s)
    first = calls["n"]
    pb.fetch_balances(s)  # should hit cache
    assert calls["n"] == first, "second call should be served from cache"

    pb.fetch_balances(s, force_refresh=True)
    assert calls["n"] > first, "force_refresh must bypass cache"
