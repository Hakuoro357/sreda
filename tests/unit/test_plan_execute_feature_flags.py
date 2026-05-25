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
