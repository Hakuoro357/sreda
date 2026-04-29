"""Unit-tests for `WebSearchUsageCounter` — quota tracking + admin views."""

from __future__ import annotations

import base64
from pathlib import Path

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models.core import Tenant
from sreda.db.session import get_engine, get_session_factory
from sreda.services.web_search_usage import (
    GLOBAL_LIMIT,
    PER_USER_LIMIT,
    WebSearchUsageCounter,
    _current_year_month,
)


def _bootstrap(monkeypatch, tmp_path: Path, name: str = "ws_usage.db"):
    db_path = tmp_path / name
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    session.add(Tenant(id="t1", name="Tenant One"))
    session.add(Tenant(id="t2", name="Tenant Two"))
    session.commit()
    return session


# --------------------------------------------------------------------
# Get-or-create + record
# --------------------------------------------------------------------


def test_record_tavily_creates_row_on_first_call(monkeypatch, tmp_path):
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        counter = WebSearchUsageCounter(session)
        counter.record_tavily(tenant_id="t1", user_id="u1")
        counter.record_tavily(tenant_id="t1", user_id="u1")

        from sreda.db.models.web_search import WebSearchUsage
        rows = session.query(WebSearchUsage).all()
    finally:
        session.close()

    assert len(rows) == 1
    assert rows[0].tenant_id == "t1"
    assert rows[0].user_id == "u1"
    assert rows[0].tavily_calls == 2
    assert rows[0].fallback_calls == 0


def test_record_fallback_does_not_increment_tavily(monkeypatch, tmp_path):
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        counter = WebSearchUsageCounter(session)
        counter.record_tavily(tenant_id="t1", user_id="u1")
        counter.record_fallback(tenant_id="t1", user_id="u1")
        counter.record_fallback(tenant_id="t1", user_id="u1")

        from sreda.db.models.web_search import WebSearchUsage
        row = session.query(WebSearchUsage).one()
    finally:
        session.close()

    assert row.tavily_calls == 1
    assert row.fallback_calls == 2


def test_no_op_without_tenant_or_user(monkeypatch, tmp_path):
    """Без tenant_id/user_id — counter молча no-op (не падает)."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        counter = WebSearchUsageCounter(session)
        counter.record_tavily(tenant_id="", user_id="u1")
        counter.record_tavily(tenant_id="t1", user_id="")
        counter.record_fallback(tenant_id=None, user_id="u1")  # type: ignore[arg-type]

        from sreda.db.models.web_search import WebSearchUsage
        rows = session.query(WebSearchUsage).all()
    finally:
        session.close()

    assert len(rows) == 0


# --------------------------------------------------------------------
# Quota: per-user
# --------------------------------------------------------------------


def test_can_use_tavily_blocks_at_per_user_limit(monkeypatch, tmp_path):
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        counter = WebSearchUsageCounter(session)
        # Bump до per-user limit
        for _ in range(PER_USER_LIMIT):
            counter.record_tavily(tenant_id="t1", user_id="u1")

        # Юзер u1 — заблокирован, u2 — свободен
        assert counter.can_use_tavily(tenant_id="t1", user_id="u1") is False
        assert counter.can_use_tavily(tenant_id="t1", user_id="u2") is True
    finally:
        session.close()


# --------------------------------------------------------------------
# Quota: global
# --------------------------------------------------------------------


def test_can_use_tavily_blocks_at_global_limit(monkeypatch, tmp_path):
    """Если глобально использовано >= GLOBAL_LIMIT — все юзеры
    блокируются, даже если у конкретного юзера ещё запас."""
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        # Распределяем GLOBAL_LIMIT по нескольким юзерам, каждый в
        # пределах своего PER_USER_LIMIT.
        counter = WebSearchUsageCounter(session)
        # 950 = 32 юзера × 30 each. Чтобы тест был быстрым, накачиваем
        # счётчики напрямую через record_tavily в цикле.
        for u in range(GLOBAL_LIMIT // PER_USER_LIMIT + 1):
            for _ in range(PER_USER_LIMIT):
                counter.record_tavily(tenant_id="t1", user_id=f"u{u}")

        # Новый юзер u_fresh (никогда не вызывал) — должен быть
        # заблокирован глобальной квотой
        assert counter.can_use_tavily(tenant_id="t1", user_id="u_fresh") is False
    finally:
        session.close()


# --------------------------------------------------------------------
# Admin views
# --------------------------------------------------------------------


def test_admin_summary_returns_current_month_totals(monkeypatch, tmp_path):
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        counter = WebSearchUsageCounter(session)
        counter.record_tavily(tenant_id="t1", user_id="u1")
        counter.record_tavily(tenant_id="t1", user_id="u1")
        counter.record_tavily(tenant_id="t1", user_id="u2")
        counter.record_fallback(tenant_id="t1", user_id="u1")

        s = counter.admin_summary()
    finally:
        session.close()

    assert s.year_month == _current_year_month()
    assert s.tavily_calls_total == 3
    assert s.fallback_calls_total == 1
    assert s.tavily_remaining == GLOBAL_LIMIT - 3
    assert s.global_limit == GLOBAL_LIMIT
    assert s.per_user_limit == PER_USER_LIMIT


def test_admin_per_user_returns_rows_sorted_descending(monkeypatch, tmp_path):
    session = _bootstrap(monkeypatch, tmp_path)
    try:
        counter = WebSearchUsageCounter(session)
        # u1: 5 tavily, u2: 10 tavily, u3: 2 tavily — u2 первым
        for _ in range(5):
            counter.record_tavily(tenant_id="t1", user_id="u1")
        for _ in range(10):
            counter.record_tavily(tenant_id="t1", user_id="u2")
        for _ in range(2):
            counter.record_tavily(tenant_id="t2", user_id="u3")

        rows = counter.admin_per_user()
    finally:
        session.close()

    assert len(rows) == 3
    assert [r.user_id for r in rows] == ["u2", "u1", "u3"]
    assert rows[0].tenant_name == "Tenant One"
    assert rows[0].tavily_calls == 10
    assert rows[0].user_remaining == PER_USER_LIMIT - 10
    assert rows[2].tenant_name == "Tenant Two"
    assert rows[2].tavily_calls == 2
