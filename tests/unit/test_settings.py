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


# ---------------------------------------------------------------------------
# Plan-Execute LLM provider split (vex-assistant#77 item #5)
# ---------------------------------------------------------------------------


def test_planner_provider_default() -> None:
    """Planner needs heavyweight reasoning + json-schema compliance —
    default tier is mimo-v2.5 (provider key) which resolves to
    mimo-v2.5-pro model (services/llm.py:1187 ``_MIMO_MODEL_BY_PROVIDER``).

    Codex Sub-A12 R1 CRITICAL: earlier default was the MODEL name
    ``"mimo-v2.5-pro"``, but ``get_chat_llm`` expects PROVIDER KEY.
    Returns None for any non-key value → planner runtime hard-fails."""
    settings = Settings()
    assert settings.planner_provider == "mimo-v2.5"


def test_planner_provider_override_via_env_alias(monkeypatch) -> None:
    """SREDA_PLANNER_PROVIDER env var overrides the default."""
    monkeypatch.setenv("SREDA_PLANNER_PROVIDER", "mimo-v2.5")
    settings = Settings()
    assert settings.planner_provider == "mimo-v2.5"


def test_composer_provider_default() -> None:
    """Composer LLM-path writes free text — a light model is fine.
    Default mimo-flash is ~3x cheaper than the planner tier."""
    settings = Settings()
    assert settings.composer_provider == "mimo-flash"


def test_composer_provider_override_via_env_alias(monkeypatch) -> None:
    """SREDA_COMPOSER_PROVIDER env var overrides the default."""
    monkeypatch.setenv("SREDA_COMPOSER_PROVIDER", "mimo-v2.5-light")
    settings = Settings()
    assert settings.composer_provider == "mimo-v2.5-light"


def test_planner_and_composer_providers_are_independent(monkeypatch) -> None:
    """Setting one shouldn't affect the other — they're independent
    knobs (whole point of the split)."""
    monkeypatch.setenv("SREDA_PLANNER_PROVIDER", "mimo-v2.5-pro")
    monkeypatch.setenv("SREDA_COMPOSER_PROVIDER", "mimo-flash")
    settings = Settings()
    assert settings.planner_provider == "mimo-v2.5-pro"
    assert settings.composer_provider == "mimo-flash"


def test_providers_accept_arbitrary_string(monkeypatch) -> None:
    """No validation on provider values — registry resolution happens
    at get_chat_llm() call time. Lets us hot-swap providers via env
    var without settings schema churn (e.g. add 'qwen-turbo' as future
    fallback). Validated through env var since the field uses
    ``AliasChoices`` (kwargs-init bypasses the alias)."""
    monkeypatch.setenv("SREDA_PLANNER_PROVIDER", "future-provider-xyz")
    monkeypatch.setenv("SREDA_COMPOSER_PROVIDER", "another-future-provider")
    settings = Settings()
    assert settings.planner_provider == "future-provider-xyz"
    assert settings.composer_provider == "another-future-provider"
