"""Tests for admin auth hardening (Phase 5 cloud migration).

2026-04-28: добавили header-based auth (X-Admin-Token), backwards-compat
с query param, timing-safe compare через hmac.compare_digest.
"""

from __future__ import annotations

import hmac
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from sreda.admin.auth import require_admin_token


def _mk_request(client_ip="127.0.0.1", path="/admin/users"):
    """Mock minimal Request object for tests."""
    req = MagicMock()
    req.headers = {}
    req.url.path = path
    req.client = MagicMock()
    req.client.host = client_ip
    return req


def test_403_when_admin_disabled(monkeypatch):
    """admin_token не сконфигурирован → 403."""
    from sreda.config.settings import Settings
    monkeypatch.setattr(
        "sreda.admin.auth.get_settings",
        lambda: Settings(admin_token=None),
    )
    req = _mk_request()
    with pytest.raises(HTTPException) as exc:
        require_admin_token(req, header_token=None, query_token=None)
    assert exc.value.status_code == 403
    assert "disabled" in exc.value.detail.lower()


def test_401_when_no_token(monkeypatch):
    from sreda.config.settings import Settings
    monkeypatch.setattr(
        "sreda.admin.auth.get_settings",
        lambda: Settings(admin_token="real-secret-token-xyz"),
    )
    req = _mk_request()
    with pytest.raises(HTTPException) as exc:
        require_admin_token(req, header_token=None, query_token=None)
    assert exc.value.status_code == 401


def test_401_when_wrong_token(monkeypatch):
    from sreda.config.settings import Settings
    monkeypatch.setattr(
        "sreda.admin.auth.get_settings",
        lambda: Settings(admin_token="real-secret-token-xyz"),
    )
    req = _mk_request()
    with pytest.raises(HTTPException) as exc:
        require_admin_token(req, header_token="wrong-token", query_token=None)
    assert exc.value.status_code == 401


def test_passes_with_correct_header_token(monkeypatch):
    from sreda.config.settings import Settings
    monkeypatch.setattr(
        "sreda.admin.auth.get_settings",
        lambda: Settings(admin_token="real-secret-token-xyz"),
    )
    req = _mk_request()
    result = require_admin_token(
        req, header_token="real-secret-token-xyz", query_token=None
    )
    assert result == "real-secret-token-xyz"


def test_passes_with_correct_query_token_legacy(monkeypatch):
    """Legacy backward compat: query param ?token= по-прежнему работает."""
    from sreda.config.settings import Settings
    monkeypatch.setattr(
        "sreda.admin.auth.get_settings",
        lambda: Settings(admin_token="real-secret-token-xyz"),
    )
    req = _mk_request()
    result = require_admin_token(
        req, header_token=None, query_token="real-secret-token-xyz"
    )
    assert result == "real-secret-token-xyz"


def test_header_wins_over_query_when_both_set(monkeypatch):
    """Header имеет приоритет — даже если query тоже есть, должен
    использоваться header. Если header верный → ОК."""
    from sreda.config.settings import Settings
    monkeypatch.setattr(
        "sreda.admin.auth.get_settings",
        lambda: Settings(admin_token="real-token"),
    )
    req = _mk_request()
    result = require_admin_token(
        req, header_token="real-token", query_token="wrong-query",
    )
    assert result == "real-token"


def test_header_wrong_query_correct_returns_401(monkeypatch):
    """Если header задан но wrong, query НЕ FALLBACK'ится. Header
    предпочтительный — он принимается как presented и проверяется."""
    from sreda.config.settings import Settings
    monkeypatch.setattr(
        "sreda.admin.auth.get_settings",
        lambda: Settings(admin_token="real-token"),
    )
    req = _mk_request()
    with pytest.raises(HTTPException) as exc:
        require_admin_token(
            req, header_token="wrong-header", query_token="real-token",
        )
    assert exc.value.status_code == 401


def test_timing_safe_compare_short_token_doesnt_crash(monkeypatch):
    """compare_digest на разных длинах → возвращает False, не crash."""
    from sreda.config.settings import Settings
    monkeypatch.setattr(
        "sreda.admin.auth.get_settings",
        lambda: Settings(admin_token="long-secret-token-12345"),
    )
    req = _mk_request()
    with pytest.raises(HTTPException) as exc:
        require_admin_token(req, header_token="short", query_token=None)
    assert exc.value.status_code == 401


def test_x_forwarded_for_used_for_logging(monkeypatch):
    """Behind nginx/proxy — берём X-Forwarded-For (первый IP)."""
    from sreda.config.settings import Settings
    monkeypatch.setattr(
        "sreda.admin.auth.get_settings",
        lambda: Settings(admin_token="ok-token"),
    )
    req = _mk_request(client_ip="10.0.0.1")
    req.headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1"}

    # Should not crash. Just success path.
    result = require_admin_token(
        req, header_token="ok-token", query_token=None,
    )
    assert result == "ok-token"
