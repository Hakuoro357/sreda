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
    default is the pro tier. After the 2026-05-29 rename the provider
    key == model name, so the pro key is ``"mimo-v2.5-pro"`` (was the
    confusing ``"mimo-v2.5"`` which silently mapped to -pro)."""
    settings = Settings()
    assert settings.planner_provider == "mimo-v2.5-pro"


def test_planner_provider_override_via_env_alias(monkeypatch) -> None:
    """SREDA_PLANNER_PROVIDER env var overrides the default."""
    monkeypatch.setenv("SREDA_PLANNER_PROVIDER", "mimo-v2.5-pro")
    settings = Settings()
    assert settings.planner_provider == "mimo-v2.5-pro"


# ---------------------------------------------------------------------------
# Sub-A12 Phase B.5 — new planner-orchestrator settings
# ---------------------------------------------------------------------------


def test_planner_prompt_version_default() -> None:
    """Default prompt version = 1; pinned in planner_executions rows."""
    settings = Settings()
    assert settings.planner_prompt_version == 1


def test_planner_prompt_version_override_via_env(monkeypatch) -> None:
    monkeypatch.setenv("SREDA_PLANNER_PROMPT_VERSION", "3")
    settings = Settings()
    assert settings.planner_prompt_version == 3


def test_planner_prompt_version_rejects_zero(monkeypatch) -> None:
    """Version must be >=1 (Field ge=1)."""
    import pytest
    monkeypatch.setenv("SREDA_PLANNER_PROMPT_VERSION", "0")
    with pytest.raises(Exception):  # noqa: BLE001 — pydantic ValidationError
        Settings()


def test_planner_invalid_retry_enabled_default() -> None:
    """Production default: retry on parse/validate failure once."""
    settings = Settings()
    assert settings.planner_invalid_retry_enabled is True


def test_planner_invalid_retry_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("SREDA_PLANNER_INVALID_RETRY_ENABLED", "false")
    settings = Settings()
    assert settings.planner_invalid_retry_enabled is False


def test_planner_alerts_enabled_default_off() -> None:
    """Default OFF — library/offline mode + eval scripts don't spam
    admin channels. Production rollout sets the env var explicitly."""
    settings = Settings()
    assert settings.planner_alerts_enabled is False


def test_planner_alerts_enabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("SREDA_PLANNER_ALERTS_ENABLED", "true")
    settings = Settings()
    assert settings.planner_alerts_enabled is True


def test_composer_provider_default() -> None:
    """Composer LLM-path writes free text — a lighter model is fine.
    After the 2026-05-29 rename the default is the plain 'mimo-v2.5'
    (provider key == model; not the pro tier, not the unavailable
    mimo-flash)."""
    settings = Settings()
    assert settings.composer_provider == "mimo-v2.5"


def test_composer_provider_override_via_env_alias(monkeypatch) -> None:
    """SREDA_COMPOSER_PROVIDER env var overrides the default."""
    monkeypatch.setenv("SREDA_COMPOSER_PROVIDER", "mimo-v2.5-pro")
    settings = Settings()
    assert settings.composer_provider == "mimo-v2.5-pro"


def test_composer_llm_enabled_keys_default_empty() -> None:
    """Per-key allow-list default EMPTY — planner-flow ships
    template-only; LLM keys are enabled one at a time (Codex
    D.2-enable R2)."""
    assert Settings().composer_llm_enabled_keys == frozenset()


def test_composer_llm_enabled_keys_via_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "SREDA_COMPOSER_LLM_ENABLED_KEYS",
        "recipe_narrative, cooking_explanation",
    )
    assert Settings().composer_llm_enabled_keys == frozenset(
        {"recipe_narrative", "cooking_explanation"}
    )


def test_composer_llm_enabled_keys_blank_is_empty(monkeypatch) -> None:
    monkeypatch.setenv("SREDA_COMPOSER_LLM_ENABLED_KEYS", "  ,  ")
    assert Settings().composer_llm_enabled_keys == frozenset()


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
