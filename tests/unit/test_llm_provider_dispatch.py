"""Tests for chat-LLM provider dispatch + fallback-chain wiring.

Covers ``get_chat_llm`` returning a MiMo/OpenRouter client based on
``chat_provider``, plus the ``.with_fallbacks([...])`` wrap when a
fallback provider is configured. No real API calls — we monkey-patch
``ChatOpenAI`` so tests stay offline and fast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from sreda.services import llm as llm_module


@dataclass
class _FakeLLM:
    """Stand-in for ChatOpenAI — stores init kwargs so tests can assert
    on base_url/model/etc. Implements ``with_fallbacks`` the same way
    langchain-core does, returning a new wrapper object."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.0
    timeout: float = 0.0
    extra: dict = field(default_factory=dict)

    def with_fallbacks(self, fallbacks: list[Any]) -> "_FakeFallback":
        return _FakeFallback(primary=self, fallbacks=list(fallbacks))


@dataclass
class _FakeFallback:
    primary: _FakeLLM
    fallbacks: list[Any]


def _fake_chat_openai_factory(**kwargs: Any) -> _FakeLLM:
    return _FakeLLM(
        base_url=kwargs.get("base_url", ""),
        api_key=str(kwargs.get("api_key") or ""),
        model=kwargs.get("model", ""),
        temperature=kwargs.get("temperature", 0.0),
        timeout=kwargs.get("timeout", 0.0),
        extra={
            k: v for k, v in kwargs.items()
            if k not in {"base_url", "api_key", "model", "temperature", "timeout"}
        },
    )


@pytest.fixture(autouse=True)
def _patch_chat_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "ChatOpenAI", _fake_chat_openai_factory)
    # Strip any SREDA_* env leakage so Settings constructs cleanly
    # from explicit overrides only — otherwise a dev shell with
    # real keys configured makes these tests order-dependent.
    import os

    for var in list(os.environ):
        if var.startswith("SREDA_"):
            monkeypatch.delenv(var, raising=False)


def _settings(**overrides: Any):
    """Build a Settings instance with the given overrides, bypassing
    env-var resolution. Anything not overridden uses the pydantic
    default from Settings itself. Blank-string defaults are applied
    for secret-holder fields so leftover env vars can't sneak a key
    into a test that explicitly wants "no key"."""
    from sreda.config.settings import Settings

    clean_defaults: dict[str, Any] = {
        "mimo_api_key": None,
        "mimo_api_key_file": None,
        "openrouter_api_key": None,
        "openrouter_api_key_file": None,
        "chat_fallback_provider": None,
    }
    clean_defaults.update(overrides)
    return Settings(**clean_defaults)


# ---------------------------------------------------------------------------
# Primary-only paths
# ---------------------------------------------------------------------------


def test_default_provider_is_mimo() -> None:
    s = _settings(mimo_api_key="mimo-k")
    got = llm_module.get_chat_llm(s)
    assert isinstance(got, _FakeLLM)
    assert got.model == "mimo-v2-pro"
    assert "xiaomimimo" in got.base_url


def test_explicit_openrouter_provider() -> None:
    s = _settings(
        chat_provider="openrouter",
        openrouter_api_key="or-k",
    )
    got = llm_module.get_chat_llm(s)
    assert isinstance(got, _FakeLLM)
    assert got.model == "google/gemma-4-26b-a4b-it"
    assert "openrouter.ai" in got.base_url


def test_provider_override_arg_wins_over_settings() -> None:
    """Admin LLM-switcher will pass ``provider=...`` for live-probing
    without persisting the change. Settings stay MiMo but the call
    returns OpenRouter."""
    s = _settings(
        mimo_api_key="mimo-k",
        openrouter_api_key="or-k",
    )
    got = llm_module.get_chat_llm(s, provider="openrouter")
    assert isinstance(got, _FakeLLM)
    assert "openrouter.ai" in got.base_url


def test_missing_key_returns_none() -> None:
    s = _settings()  # no keys anywhere
    assert llm_module.get_chat_llm(s) is None


def test_unknown_provider_returns_none() -> None:
    s = _settings(chat_provider="anthropic-claude-direct")
    assert llm_module.get_chat_llm(s) is None


def test_openrouter_missing_key_returns_none() -> None:
    s = _settings(chat_provider="openrouter")  # no OR key
    assert llm_module.get_chat_llm(s) is None


# ---------------------------------------------------------------------------
# Fallback wiring
# ---------------------------------------------------------------------------


def test_with_fallback_disabled_by_default() -> None:
    """Absence of ``chat_fallback_provider`` must never trigger the
    wrap — a no-op config shouldn't change runtime behaviour."""
    s = _settings(mimo_api_key="mimo-k")
    got = llm_module.get_chat_llm(s, with_fallback=True)
    assert isinstance(got, _FakeLLM)
    assert not isinstance(got, _FakeFallback)


def test_with_fallback_enabled_wraps_both_providers() -> None:
    s = _settings(
        mimo_api_key="mimo-k",
        openrouter_api_key="or-k",
        chat_fallback_provider="openrouter",
    )
    got = llm_module.get_chat_llm(s, with_fallback=True)
    assert isinstance(got, _FakeFallback)
    assert "xiaomimimo" in got.primary.base_url  # primary = mimo
    assert len(got.fallbacks) == 1
    assert "openrouter.ai" in got.fallbacks[0].base_url


def test_with_fallback_same_as_primary_is_noop() -> None:
    """Config smell: fallback equals primary. Must skip the wrap and
    log a warning instead of producing a self-referential chain."""
    s = _settings(
        mimo_api_key="mimo-k",
        chat_fallback_provider="mimo",
    )
    got = llm_module.get_chat_llm(s, with_fallback=True)
    assert isinstance(got, _FakeLLM)
    assert not isinstance(got, _FakeFallback)


def test_with_fallback_missing_fallback_key_degrades_to_primary() -> None:
    """Fallback provider configured but key missing — keep serving
    requests with the primary rather than crashing the turn."""
    s = _settings(
        mimo_api_key="mimo-k",
        chat_fallback_provider="openrouter",  # no OR key
    )
    got = llm_module.get_chat_llm(s, with_fallback=True)
    assert isinstance(got, _FakeLLM)
    assert not isinstance(got, _FakeFallback)


def test_with_fallback_primary_missing_returns_none() -> None:
    """If the primary itself isn't configurable, fallback doesn't
    matter — whole chat feature is disabled for this install."""
    s = _settings(
        chat_provider="openrouter",  # no OR key
        chat_fallback_provider="mimo",
        mimo_api_key="mimo-k",  # fallback would be available, but primary is the lead
    )
    assert llm_module.get_chat_llm(s, with_fallback=True) is None


# ---------------------------------------------------------------------------
# resolve_openrouter_api_key file-fallback
# ---------------------------------------------------------------------------


def test_resolve_openrouter_api_key_from_file(tmp_path) -> None:
    token_path = tmp_path / "openrouter.md"
    token_path.write_text("\n\nsk-or-v1-abc123\nSome trailing notes\n", encoding="utf-8")
    s = _settings(openrouter_api_key_file=str(token_path))
    assert s.resolve_openrouter_api_key() == "sk-or-v1-abc123"


def test_resolve_openrouter_api_key_env_beats_file(tmp_path) -> None:
    token_path = tmp_path / "openrouter.md"
    token_path.write_text("file-value", encoding="utf-8")
    s = _settings(
        openrouter_api_key="env-value",
        openrouter_api_key_file=str(token_path),
    )
    assert s.resolve_openrouter_api_key() == "env-value"
