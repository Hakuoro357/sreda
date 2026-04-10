"""Settings validation tests.

These lock the contract for security-sensitive config fields. The
``connect_public_base_url`` in particular gets embedded into one-time
connect links we send to end users in Telegram, so a misconfigured
or attacker-controlled value would be an open-redirect / phishing
vector for EDS credentials.
"""

from __future__ import annotations

import pytest

from sreda.config.settings import Settings


def test_connect_public_base_url_accepts_https() -> None:
    settings = Settings(connect_public_base_url="https://connect.example.com")
    assert settings.connect_public_base_url == "https://connect.example.com"


def test_connect_public_base_url_accepts_https_test_tld() -> None:
    # The test suite uses .test TLDs in fixtures — must keep working.
    settings = Settings(connect_public_base_url="https://connect.example.test")
    assert settings.connect_public_base_url == "https://connect.example.test"


def test_connect_public_base_url_accepts_http_localhost_for_dev() -> None:
    # Local development against a plain HTTP server must remain possible.
    settings = Settings(connect_public_base_url="http://localhost:8000")
    assert settings.connect_public_base_url == "http://localhost:8000"


def test_connect_public_base_url_accepts_none() -> None:
    settings = Settings(connect_public_base_url=None)
    assert settings.connect_public_base_url is None


def test_connect_public_base_url_rejects_plain_http_public_host() -> None:
    # Public HTTP would let one-time tokens travel over the wire in
    # plaintext, and any downstream open-redirect via misconfig would
    # phish EDS credentials.
    with pytest.raises(ValueError):
        Settings(connect_public_base_url="http://connect.example.com")


def test_connect_public_base_url_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError):
        Settings(connect_public_base_url="javascript:alert(1)")


def test_connect_public_base_url_rejects_missing_host() -> None:
    with pytest.raises(ValueError):
        Settings(connect_public_base_url="https://")


def test_connect_public_base_url_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        Settings(connect_public_base_url="not a url")
