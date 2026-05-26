"""Tests for ``runtime.graph._make_checkpointer`` selection logic (Sub-A6).

Sub-A6 of Plan-Execute Epic (#74) — lift the checkpointer from a hard
``InMemorySaver()`` line to a real ``PostgresSaver`` when the database
URL points at Postgres, with explicit memory-override env var for
tests / dev / sqlite paths.

We don't spin up real Postgres in unit tests — PostgresSaver
construction is mocked via patching ``_build_postgres_checkpointer``.
The actual recovery behaviour lives in integration tests (Phase E
smoke after Sub-A6 lands on VDS).

Behaviour matrix verified here:

  db_url               | override     | result
  --------------------- | ------------ | -------------------------
  sqlite:///...        | -            | InMemorySaver
  postgresql://...     | -            | PostgresSaver
  postgresql://...     | "memory"     | InMemorySaver
  postgresql://...     | "MEMORY"     | InMemorySaver (case-insensitive)
  postgresql+psycopg://| -            | PostgresSaver
  (singleton)          | -            | same instance on repeat calls
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sreda.runtime import graph as graph_module


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Clear the module-level PostgresSaver singleton between tests so
    one test's mock doesn't leak into the next."""
    graph_module._POSTGRES_CHECKPOINTER_SINGLETON = None
    yield
    graph_module._POSTGRES_CHECKPOINTER_SINGLETON = None


# ---------------------------------------------------------------------------
# sqlite / dev fallback
# ---------------------------------------------------------------------------


def test_sqlite_url_returns_in_memory_saver(monkeypatch) -> None:
    monkeypatch.setenv("SREDA_DATABASE_URL", "sqlite:///tmp/test.db")
    monkeypatch.delenv("SREDA_LANGGRAPH_CHECKPOINTER", raising=False)
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    try:
        saver = graph_module._make_checkpointer()
        assert isinstance(saver, InMemorySaver)
    finally:
        get_settings.cache_clear()


def test_empty_database_url_returns_in_memory_saver(monkeypatch) -> None:
    monkeypatch.setenv("SREDA_DATABASE_URL", "")
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    try:
        saver = graph_module._make_checkpointer()
        assert isinstance(saver, InMemorySaver)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Memory override — even with postgres URL, env var forces memory
# ---------------------------------------------------------------------------


def test_override_memory_forces_in_memory(monkeypatch) -> None:
    monkeypatch.setenv(
        "SREDA_DATABASE_URL", "postgresql://user:pw@host/db"
    )
    monkeypatch.setenv("SREDA_LANGGRAPH_CHECKPOINTER", "memory")
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    try:
        saver = graph_module._make_checkpointer()
        assert isinstance(saver, InMemorySaver)
    finally:
        get_settings.cache_clear()


def test_override_memory_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv(
        "SREDA_DATABASE_URL", "postgresql://user:pw@host/db"
    )
    monkeypatch.setenv("SREDA_LANGGRAPH_CHECKPOINTER", "MEMORY")
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    try:
        saver = graph_module._make_checkpointer()
        assert isinstance(saver, InMemorySaver)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Postgres path — mocked construction (real PG only in integration tests)
# ---------------------------------------------------------------------------


def test_postgres_url_builds_postgres_saver(monkeypatch) -> None:
    monkeypatch.setenv(
        "SREDA_DATABASE_URL", "postgresql://user:pw@host/db"
    )
    monkeypatch.delenv("SREDA_LANGGRAPH_CHECKPOINTER", raising=False)
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    fake_saver = MagicMock(name="FakePostgresSaver")
    try:
        with patch.object(
            graph_module,
            "_build_postgres_checkpointer",
            return_value=fake_saver,
        ) as builder:
            saver = graph_module._make_checkpointer()
        assert saver is fake_saver
        builder.assert_called_once_with("postgresql://user:pw@host/db")
    finally:
        get_settings.cache_clear()


def test_postgres_psycopg_driver_url_also_recognized(monkeypatch) -> None:
    """``postgresql+psycopg://`` — SQLAlchemy-style driver suffix is
    common in dev .env files and should map to the Postgres path."""
    monkeypatch.setenv(
        "SREDA_DATABASE_URL", "postgresql+psycopg://u:p@h/d"
    )
    monkeypatch.delenv("SREDA_LANGGRAPH_CHECKPOINTER", raising=False)
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    fake_saver = MagicMock(name="FakePostgresSaver")
    try:
        with patch.object(
            graph_module,
            "_build_postgres_checkpointer",
            return_value=fake_saver,
        ) as builder:
            saver = graph_module._make_checkpointer()
        assert saver is fake_saver
        assert builder.called
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Singleton — pool isn't recreated on repeat calls
# ---------------------------------------------------------------------------


def test_postgres_saver_is_singleton_across_calls(monkeypatch) -> None:
    """Connection pool teardown + setup on every graph compile would
    crater latency. We hold the saver process-wide."""
    monkeypatch.setenv(
        "SREDA_DATABASE_URL", "postgresql://user:pw@host/db"
    )
    monkeypatch.delenv("SREDA_LANGGRAPH_CHECKPOINTER", raising=False)
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    fake_saver = MagicMock(name="FakePostgresSaver")
    try:
        with patch.object(
            graph_module,
            "_build_postgres_checkpointer",
            return_value=fake_saver,
        ) as builder:
            saver1 = graph_module._make_checkpointer()
            saver2 = graph_module._make_checkpointer()
            saver3 = graph_module._make_checkpointer()
        assert saver1 is saver2 is saver3
        # builder runs ONCE — subsequent calls return the cached singleton
        builder.assert_called_once()
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# State persistence (smoke — only sqlite/memory path actually exercises here)
# ---------------------------------------------------------------------------


def test_in_memory_saver_smoke_persists_within_lifetime(monkeypatch) -> None:
    """Trivial smoke — InMemorySaver returned by _make_checkpointer
    actually behaves as a checkpointer (has .get/.put methods).

    Forces memory override since the conftest default settings have a
    postgres URL we don't want to actually connect to. Real cross-
    process persistence is verified by Phase E smoke on VDS once
    Sub-A6 lands.
    """
    monkeypatch.setenv("SREDA_LANGGRAPH_CHECKPOINTER", "memory")
    saver = graph_module._make_checkpointer()
    assert hasattr(saver, "get")
    assert hasattr(saver, "put")


def test_sqlalchemy_psycopg_url_stripped_for_libpq(monkeypatch) -> None:
    """psycopg's ConnectionPool wants a plain ``postgresql://`` libpq
    URL — SQLAlchemy's ``postgresql+psycopg://`` driver suffix breaks
    it with 'missing "=" after ...'. Stripping happens inside
    ``_build_postgres_checkpointer``; verify via mocked ConnectionPool."""
    from unittest.mock import patch as _patch

    monkeypatch.setenv("SREDA_LANGGRAPH_POOL_MAX_SIZE", "5")
    with (
        _patch("psycopg_pool.ConnectionPool") as PoolMock,
        _patch("langgraph.checkpoint.postgres.PostgresSaver") as SaverMock,
    ):
        PoolMock.return_value = MagicMock(name="pool")
        SaverMock.return_value = MagicMock(name="saver")
        graph_module._build_postgres_checkpointer(
            "postgresql+psycopg://u:p@h/d"
        )
    PoolMock.assert_called_once()
    kwargs = PoolMock.call_args.kwargs
    assert kwargs["conninfo"] == "postgresql://u:p@h/d", (
        "psycopg suffix not stripped: " + kwargs["conninfo"]
    )
    assert kwargs["max_size"] == 5
