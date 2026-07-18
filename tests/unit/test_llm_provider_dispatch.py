"""Tests for chat-LLM provider dispatch + fallback-chain wiring.

Covers ``get_chat_llm`` returning a MiMo/OpenRouter client based on
``chat_provider``, plus the ``.with_fallbacks([...])`` wrap when a
fallback provider is configured. No real API calls — we monkey-patch
``ChatOpenAI`` so tests stay offline and fast.

2026-05-23 — 2026-07-18: файл был целиком скипнут (#66): тесты делали
реальные network calls и pytest вис >60s. Корневая причина (аудит
2026-07-18, MAJOR): autouse-фикстура патчила ``llm_module.ChatOpenAI``,
но MiMo-путь в ``llm._build_chat_llm`` ЛЕНИВО импортирует
``MimoChatOpenAI`` из ``sreda.services.mimo_chat_openai`` (R-29) —
патч на ``llm_module`` его не доставал, конструировался настоящий
клиент. Починка: патчим ``MimoChatOpenAI`` в модуле-источнике (ленивый
импорт резолвит атрибут модуля в момент вызова) + локальный
network-guard ниже — любой возврат к реальной сети падает мгновенно
с AssertionError вместо зависания (в unit-сьюите общего гарда нет —
он живёт только в tests/functional/conftest.py).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.models.runtime_config import RuntimeConfig
from sreda.services import llm as llm_module
from sreda.services import runtime_config as rc


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
    # MiMo-путь в llm._build_chat_llm ЛЕНИВО импортирует MimoChatOpenAI
    # (``from sreda.services.mimo_chat_openai import MimoChatOpenAI`` внутри
    # функции, R-29) — патч на llm_module.ChatOpenAI его не доставал,
    # из-за чего тесты когда-то уходили в реальную сеть (#66). Ленивый
    # импорт резолвит атрибут модуля-источника в момент вызова → патчим там.
    import sreda.services.mimo_chat_openai as mimo_module

    monkeypatch.setattr(
        mimo_module, "MimoChatOpenAI", _fake_chat_openai_factory,
    )
    # Strip any SREDA_* env leakage so Settings constructs cleanly
    # from explicit overrides only — otherwise a dev shell with
    # real keys configured makes these tests order-dependent.
    import os

    for var in list(os.environ):
        if var.startswith("SREDA_"):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _offline_provider_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_chat_llm`` без ``provider=`` делает DB-lookup admin-switcher'а
    (``runtime_config``) через глобальную ``get_session_factory`` — с
    дефолтным PG-DSN это реальный network connect (psycopg/libpq идёт на
    C-уровне МИМО socket-патчей) — историческое зависание >60s (#66).
    Шов ``_factory_for`` — тот же, что в ``worker_db`` (unit conftest):
    подменяем фабрику на локальную SQLite с ПУСТОЙ ``runtime_config`` →
    lookup честно отрабатывает «нет записи» → env-дефолты, офлайн."""
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    RuntimeConfig.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("sreda.db.session._factory_for", lambda _engine: factory)


@pytest.fixture(autouse=True)
def _no_network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Локальный network-guard (аудит 2026-07-18): в unit-сьюите общего
    гарда нет, а этот модуль ровно про конструирование LLM-клиентов.
    Любая регрессия к реальному сетевому вызову падает мгновенно с
    AssertionError вместо исторического зависания >60s (#66 / R-29)."""

    def _blocked(*a: Any, **kw: Any) -> None:
        raise AssertionError(
            "внешняя сеть запрещена в test_llm_provider_dispatch "
            f"(offline-гарда): {a!r} {kw!r}"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    import httpx

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", _blocked,
    )
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)


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


@pytest.fixture
def runtime_session():
    engine = create_engine("sqlite:///:memory:")
    RuntimeConfig.__table__.create(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _set_runtime_config(session, key: str, value: str) -> None:
    session.add(RuntimeConfig(key=key, value=value))
    session.commit()
    rc.invalidate_cache()


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


def test_openrouter_grok_variant_overrides_model() -> None:
    """The ``openrouter-grok`` provider key points at Grok on OR —
    same base_url + key as the default openrouter, but the model id
    is hard-overridden. Lets the admin switcher expose multiple OR
    models as distinct rows without per-variant settings plumbing."""
    s = _settings(
        chat_provider="openrouter-grok",
        openrouter_api_key="or-k",
    )
    got = llm_module.get_chat_llm(s)
    assert isinstance(got, _FakeLLM)
    assert got.model == "x-ai/grok-4.1-fast"
    assert "openrouter.ai" in got.base_url


def test_openrouter_qwen_variant_overrides_model() -> None:
    s = _settings(
        chat_provider="openrouter-qwen",
        openrouter_api_key="or-k",
    )
    got = llm_module.get_chat_llm(s)
    assert isinstance(got, _FakeLLM)
    assert got.model == "qwen/qwen3.6-plus"


def test_openrouter_variants_share_the_same_key() -> None:
    """All three OpenRouter variants must degrade to None when the OR
    key isn't configured — prevents silent behaviour where the UI
    showed 'available' but runtime found no key."""
    for variant in ("openrouter", "openrouter-grok", "openrouter-qwen"):
        s_v = _settings(chat_provider=variant)
        assert llm_module.get_chat_llm(s_v) is None, variant


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
# Tenant canary routing
# ---------------------------------------------------------------------------


def test_tenant_override_wins_over_global_provider_pair(runtime_session) -> None:
    _set_runtime_config(runtime_session, rc.KEY_CHAT_PROVIDER, "mimo")
    _set_runtime_config(runtime_session, rc.KEY_CHAT_FALLBACK_PROVIDER, "")
    _set_runtime_config(
        runtime_session,
        rc.KEY_CHAT_PROVIDER_TENANT_OVERRIDES,
        '{"tenant_a": {"primary": "openrouter", "fallback": "mimo"}}',
    )
    s = _settings(mimo_api_key="mimo-k", openrouter_api_key="or-k")

    assert llm_module.resolve_provider_pair_for_tenant(
        runtime_session, "tenant_a", s,
    ) == ("openrouter", "mimo")


def test_tenant_override_bad_provider_falls_back_to_global(runtime_session) -> None:
    _set_runtime_config(runtime_session, rc.KEY_CHAT_PROVIDER, "mimo")
    _set_runtime_config(runtime_session, rc.KEY_CHAT_FALLBACK_PROVIDER, "openrouter")
    _set_runtime_config(
        runtime_session,
        rc.KEY_CHAT_PROVIDER_TENANT_OVERRIDES,
        '{"tenant_a": {"primary": "missing-provider", "fallback": "mimo"}}',
    )
    s = _settings(mimo_api_key="mimo-k", openrouter_api_key="or-k")

    assert llm_module.resolve_provider_pair_for_tenant(
        runtime_session, "tenant_a", s,
    ) == ("mimo", "openrouter")


def test_tenant_override_unavailable_provider_falls_back_to_global(runtime_session) -> None:
    _set_runtime_config(runtime_session, rc.KEY_CHAT_PROVIDER, "mimo")
    _set_runtime_config(runtime_session, rc.KEY_CHAT_FALLBACK_PROVIDER, "")
    _set_runtime_config(
        runtime_session,
        rc.KEY_CHAT_PROVIDER_TENANT_OVERRIDES,
        '{"tenant_a": {"primary": "openrouter", "fallback": "mimo"}}',
    )
    s = _settings(mimo_api_key="mimo-k")  # no OpenRouter key

    assert llm_module.resolve_provider_pair_for_tenant(
        runtime_session, "tenant_a", s,
    ) == ("mimo", None)


def test_provider_pair_session_factory_failure_falls_back_to_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sreda.db import session as db_session

    def _boom():
        raise RuntimeError("db not ready")

    monkeypatch.setattr(db_session, "get_session_factory", _boom)
    s = _settings(chat_provider="openrouter", chat_fallback_provider="mimo")

    assert llm_module.resolve_provider_pair(s) == ("openrouter", "mimo")


def test_tenant_pair_runtime_config_error_keeps_env_fallback(
    runtime_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("runtime_config not ready")

    monkeypatch.setattr(rc, "get_config", _boom)
    s = _settings(chat_provider="mimo", chat_fallback_provider="openrouter")

    assert llm_module.resolve_provider_pair_for_tenant(
        runtime_session, "tenant_a", s,
    ) == ("mimo", "openrouter")


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
