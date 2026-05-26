"""Tests for the plan-execute rollout feature flags (Sub-A2 + future).

Two independent per-tenant whitelists govern the rollout:

- ``SREDA_MESSAGE_QUEUE_ENABLED_TENANTS`` — tenants whose inbound
  messages go through the new FIFO queue (this commit's poller refactor
  reads ``is_queue_enabled_for`` to decide).
- ``SREDA_PLANNER_ENABLED_TENANTS`` — tenants whose worker-side
  processing uses planner-flow (graph-edge check lands in Sub-A6).

Both default to empty → everyone stays on the legacy inline path,
existing behaviour unchanged.
"""

from __future__ import annotations

import pytest

from sreda.config.settings import Settings
from sreda.workers.message_queue import is_queue_enabled_for


# ---------------------------------------------------------------------------
# Settings parsing
# ---------------------------------------------------------------------------


def test_queue_whitelist_empty_by_default() -> None:
    s = Settings()
    assert s.message_queue_enabled_tenants == frozenset()


def test_queue_whitelist_parses_csv() -> None:
    s = Settings(
        SREDA_MESSAGE_QUEUE_ENABLED_TENANTS="tenant_max_352612382,tenant_tg_111"
    )
    assert s.message_queue_enabled_tenants == frozenset(
        {"tenant_max_352612382", "tenant_tg_111"}
    )


def test_queue_whitelist_strips_whitespace() -> None:
    s = Settings(
        SREDA_MESSAGE_QUEUE_ENABLED_TENANTS=" tenant_a , tenant_b ,, tenant_c "
    )
    assert s.message_queue_enabled_tenants == frozenset(
        {"tenant_a", "tenant_b", "tenant_c"}
    )


def test_planner_whitelist_empty_by_default() -> None:
    s = Settings()
    assert s.planner_enabled_tenants == frozenset()
    assert s.planner_mode_enabled is False


def test_planner_whitelist_parses_csv() -> None:
    s = Settings(
        SREDA_PLANNER_ENABLED_TENANTS="tenant_max_352612382",
        SREDA_PLANNER_MODE_ENABLED=True,
    )
    assert s.planner_enabled_tenants == {"tenant_max_352612382"}
    assert s.planner_mode_enabled is True


def test_whitelists_are_independent() -> None:
    """Queue can be on without planner, vice versa."""
    s = Settings(
        SREDA_MESSAGE_QUEUE_ENABLED_TENANTS="tenant_a,tenant_b",
        SREDA_PLANNER_ENABLED_TENANTS="tenant_a",
    )
    assert "tenant_b" in s.message_queue_enabled_tenants
    assert "tenant_b" not in s.planner_enabled_tenants
    assert "tenant_a" in s.message_queue_enabled_tenants
    assert "tenant_a" in s.planner_enabled_tenants


# ---------------------------------------------------------------------------
# is_queue_enabled_for helper
# ---------------------------------------------------------------------------


def test_is_queue_enabled_for_returns_false_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SREDA_MESSAGE_QUEUE_ENABLED_TENANTS", raising=False)
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    try:
        assert is_queue_enabled_for("tenant_max_352612382") is False
        assert is_queue_enabled_for("tenant_anyone") is False
    finally:
        get_settings.cache_clear()


def test_is_queue_enabled_for_whitelisted_tenant(monkeypatch) -> None:
    monkeypatch.setenv(
        "SREDA_MESSAGE_QUEUE_ENABLED_TENANTS",
        "tenant_max_352612382,tenant_tg_test",
    )
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    try:
        assert is_queue_enabled_for("tenant_max_352612382") is True
        assert is_queue_enabled_for("tenant_tg_test") is True
        assert is_queue_enabled_for("tenant_other") is False
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# LangGraph checkpointer settings (Sub-A6 consolidated via Sub-A8)
# ---------------------------------------------------------------------------


def test_langgraph_checkpointer_mode_defaults_to_auto(monkeypatch) -> None:
    """The conftest autouse fixture force-sets the env var to ``memory``
    so unit tests don't try to hit real Postgres. Clear it here to
    verify the actual Settings default."""
    monkeypatch.delenv("SREDA_LANGGRAPH_CHECKPOINTER", raising=False)
    s = Settings()
    assert s.langgraph_checkpointer_mode == "auto"


def test_langgraph_checkpointer_mode_accepts_memory_override() -> None:
    s = Settings(SREDA_LANGGRAPH_CHECKPOINTER="memory")
    assert s.langgraph_checkpointer_mode == "memory"


def test_langgraph_pool_max_size_default_is_10() -> None:
    s = Settings()
    assert s.langgraph_pool_max_size == 10


def test_langgraph_pool_max_size_respects_env_override() -> None:
    s = Settings(SREDA_LANGGRAPH_POOL_MAX_SIZE=25)
    assert s.langgraph_pool_max_size == 25


def test_langgraph_pool_max_size_below_1_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(SREDA_LANGGRAPH_POOL_MAX_SIZE=0)


def test_langgraph_pool_max_size_above_100_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(SREDA_LANGGRAPH_POOL_MAX_SIZE=101)
