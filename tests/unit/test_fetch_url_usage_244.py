"""#244 — атомарная per-day квота fetch_url (try_consume_fetch_url, NO-REFUND).

Юнит покрывает МЕХАНИЗМ CAS-гарда (WHERE count<cap) детерминированно на SQLite:
- free cap соблюдён (ровно N резервов, N+1 → False);
- paid/grandfathered (cap=None) — без лимита;
- anonymous (нет tenant/user) → fail-closed, строки нет;
- per-day rollover (другой ymd → свежий счётчик);
- NO-REFUND контракт закодирован тестом (модуль НЕ экспортирует release_* — анти-#74 spec-drift).
Истинная гонка N+K→N (PG SERIALIZABLE) — в integration (test_fetch_url_quota_postgres_concurrency.py).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from sreda.services.fetch_url_usage import try_consume_fetch_url


@pytest.fixture
def engine():
    """Отдельный in-memory SQLite (НЕ общий _test_engine): CAS коммитит, не должен течь в другие тесты."""
    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401 — регистрирует FetchUrlUsage в metadata

    eng = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _count(engine, t="ten", u="usr", ymd="2026-06-30") -> int | None:
    with engine.begin() as c:
        return c.execute(text(
            "SELECT fetch_calls FROM fetch_url_usage "
            "WHERE tenant_id=:t AND user_id=:u AND ymd=:ymd"
        ), {"t": t, "u": u, "ymd": ymd}).scalar()


def test_free_cap_enforced_exactly_n(engine):
    cap = 3
    results = [try_consume_fetch_url(engine, "ten", "usr", cap, ymd="2026-06-30") for _ in range(5)]
    assert results == [True, True, True, False, False]
    assert _count(engine) == 3  # ровно cap, 4-й/5-й не инкрементнули


def test_cap_none_unlimited(engine):
    # paid/grandfathered — без дневного лимита, но считаем для наблюдаемости
    for _ in range(5):
        assert try_consume_fetch_url(engine, "ten", "usr", None, ymd="2026-06-30") is True
    assert _count(engine) == 5


def test_cap_zero_blocks_first(engine):
    assert try_consume_fetch_url(engine, "ten", "usr", 0, ymd="2026-06-30") is False
    assert _count(engine) is None  # строки нет — insert-путь WHERE 1<=0 ложь


def test_anonymous_fail_closed(engine):
    assert try_consume_fetch_url(engine, "", "usr", 5, ymd="2026-06-30") is False
    assert try_consume_fetch_url(engine, "ten", "", 5, ymd="2026-06-30") is False
    # ни одной строки не создано
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM fetch_url_usage")).scalar() == 0


def test_per_day_rollover_resets(engine):
    cap = 2
    assert try_consume_fetch_url(engine, "ten", "usr", cap, ymd="2026-06-30") is True
    assert try_consume_fetch_url(engine, "ten", "usr", cap, ymd="2026-06-30") is True
    assert try_consume_fetch_url(engine, "ten", "usr", cap, ymd="2026-06-30") is False  # день исчерпан
    # следующий день — свежий счётчик
    assert try_consume_fetch_url(engine, "ten", "usr", cap, ymd="2026-07-01") is True
    assert _count(engine, ymd="2026-07-01") == 1


def test_no_refund_contract_no_release_symbol():
    """NO-REFUND закодирован как контракт (анти-#74): модуль НЕ должен экспортировать release_*/refund_*."""
    import sreda.services.fetch_url_usage as mod

    bad = [n for n in dir(mod) if n.startswith(("release", "refund", "decrement"))]
    assert bad == [], f"no-refund нарушен: найдены {bad}"
