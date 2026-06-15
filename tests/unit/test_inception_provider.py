"""Offline dispatch coverage for the `inception-mercury2` provider
(Inception Mercury-2, direct api.inceptionlabs.ai). Locks the prod-flip
wiring contract: registration, model id, temperature pin, key resolution
(env→file→None), availability check, graceful None without a key, and the
client attributes when a key IS configured.

No network: we never call `.invoke()`. Constructing a `ChatOpenAI` does not
hit the API (the call happens lazily on invoke), so these stay offline/fast.
Added 2026-06-15 (#60) per the planner-migration review (Codex high+medium +
Claude subagent flagged the missing dispatch test for the flip path).
"""
from __future__ import annotations

from sreda.config.settings import Settings, get_settings
from sreda.services.llm import (
    CHAT_PROVIDERS,
    _INCEPTION_MODEL_BY_PROVIDER,
    _override_temperature,
    _provider_key_is_available,
    get_chat_llm,
)

_PROVIDER = "inception-mercury2"


def _fresh(monkeypatch, **env) -> Settings:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return Settings()


def test_inception_provider_registered():
    """Provider is in the allowlist and maps to the `mercury-2` model id."""
    assert _PROVIDER in CHAT_PROVIDERS
    assert _INCEPTION_MODEL_BY_PROVIDER[_PROVIDER] == "mercury-2"


def test_inception_temperature_pinned_to_06():
    """Mercury rejects temp<0.5 → override forces 0.6; other providers pass through."""
    assert _override_temperature(_PROVIDER, 0.1) == 0.6
    assert _override_temperature("some-other-provider", 0.3) == 0.3


def test_inception_key_resolution_env_then_file_then_none(monkeypatch, tmp_path):
    """resolve_inception_api_key precedence: explicit env → file → None."""
    # None when nothing configured
    s = _fresh(monkeypatch, SREDA_INCEPTION_API_KEY="", SREDA_INCEPTION_API_KEY_FILE="")
    assert s.resolve_inception_api_key() is None

    # File path resolves to file contents
    key_file = tmp_path / "inception.txt"
    key_file.write_text("FILE-KEY-xyz\n", encoding="utf-8")
    s = _fresh(monkeypatch, SREDA_INCEPTION_API_KEY_FILE=str(key_file))
    assert s.resolve_inception_api_key() == "FILE-KEY-xyz"

    # Explicit env key wins over file
    s = _fresh(
        monkeypatch,
        SREDA_INCEPTION_API_KEY="ENV-KEY-abc",
        SREDA_INCEPTION_API_KEY_FILE=str(key_file),
    )
    assert s.resolve_inception_api_key() == "ENV-KEY-abc"


def test_inception_availability_reflects_key(monkeypatch):
    s = _fresh(monkeypatch, SREDA_INCEPTION_API_KEY="K")
    assert _provider_key_is_available(_PROVIDER, s) is True
    s = _fresh(monkeypatch, SREDA_INCEPTION_API_KEY="", SREDA_INCEPTION_API_KEY_FILE="")
    assert _provider_key_is_available(_PROVIDER, s) is False


def test_inception_get_chat_llm_none_without_key(monkeypatch):
    """Graceful degradation: no key → get_chat_llm returns None (no client)."""
    s = _fresh(monkeypatch, SREDA_INCEPTION_API_KEY="", SREDA_INCEPTION_API_KEY_FILE="")
    assert get_chat_llm(settings=s, provider=_PROVIDER) is None


def test_inception_get_chat_llm_client_attrs_with_key(monkeypatch):
    """With a key → real ChatOpenAI pointed at Inception, model=mercury-2, temp=0.6."""
    s = _fresh(monkeypatch, SREDA_INCEPTION_API_KEY="ENV-KEY-abc")
    client = get_chat_llm(settings=s, provider=_PROVIDER)
    assert client is not None
    assert client.model_name == "mercury-2"
    assert float(client.temperature) == 0.6
    assert str(client.openai_api_base).rstrip("/") == "https://api.inceptionlabs.ai/v1"
