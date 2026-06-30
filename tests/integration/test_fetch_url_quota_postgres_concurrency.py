"""Postgres-only concurrency-тест `try_consume_fetch_url` (#244 квота, acceptance #3).

SQLite сериализует писателей → гонку на границе per-day cap НЕ ловит. Нужен реальный Postgres
с SERIALIZABLE + retry 40001. Тест: N+K одновременных free-консьюмов (cap=N) → ровно N True +
K False; счётчик в БД == N (атомарный per-day CAS не пробивается параллелью).

Запуск:
  $ SREDA_TEST_POSTGRES_URL=postgresql://user:pw@localhost/sreda_test \
    SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1 \
    PYTHONPATH=src python -m pytest \
      tests/integration/test_fetch_url_quota_postgres_concurrency.py -v

Без обоих env-var модуль скипается (SQLite-юниты остаются зелёными).
"""
from __future__ import annotations

import os
import queue
import threading

import pytest
from sqlalchemy import create_engine, text

from sreda.db.models.fetch_url_usage import FetchUrlUsage
from sreda.services.fetch_url_usage import try_consume_fetch_url

_POSTGRES_URL = os.environ.get("SREDA_TEST_POSTGRES_URL")
_DESTRUCTIVE_OPT_IN = os.environ.get("SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN") == "1"

pytestmark = pytest.mark.skipif(
    not (_POSTGRES_URL and _DESTRUCTIVE_OPT_IN),
    reason=(
        "Postgres concurrency tests require BOTH SREDA_TEST_POSTGRES_URL and "
        "SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1 — they DROP and re-create "
        "fetch_url_usage. SQLite serializes writers; Postgres SERIALIZABLE required."
    ),
)

_YMD = "2026-06-30"


@pytest.fixture
def engine():
    eng = create_engine(_POSTGRES_URL, echo=False, future=True)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS fetch_url_usage"))
        FetchUrlUsage.__table__.create(conn)
    yield eng
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS fetch_url_usage"))
    eng.dispose()


def _run_concurrent(engine, n_threads: int, *, tenant_id, user_id, per_day_cap):
    """n_threads потоков, barrier-synced для макс. contention, на ОДНОГО юзера. Returns list[bool]."""
    barrier = threading.Barrier(n_threads)
    result_q: queue.Queue = queue.Queue()

    def runner(idx: int) -> None:
        try:
            barrier.wait(timeout=10.0)
            ok = try_consume_fetch_url(engine, tenant_id, user_id, per_day_cap, _YMD)
            result_q.put(("ok", ok))
        except Exception as exc:  # noqa: BLE001
            result_q.put(("err", repr(exc)))

    threads = [threading.Thread(target=runner, args=(i,), daemon=True) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    out = []
    while not result_q.empty():
        status, payload = result_q.get_nowait()
        assert status == "ok", f"thread errored: {payload!r}"
        out.append(payload)
    return out


def test_fetch_url_quota_concurrent_exactly_n(engine):
    """8 одновременных free-консьюмов одного юзера (cap=5) → ровно 5 True + 3 False; счётчик == 5."""
    cap = 5
    decisions = _run_concurrent(engine, 8, tenant_id="t1", user_id="u1", per_day_cap=cap)
    allowed = sum(1 for d in decisions if d is True)
    denied = sum(1 for d in decisions if d is False)
    assert allowed == cap, f"expected {cap} True, got {allowed}: {decisions}"
    assert denied == 8 - cap, f"expected {8 - cap} False, got {decisions}"

    with engine.begin() as conn:
        total = conn.execute(text(
            "SELECT fetch_calls FROM fetch_url_usage "
            "WHERE tenant_id='t1' AND user_id='u1' AND ymd=:ymd"
        ), {"ymd": _YMD}).scalar()
    assert total == cap, f"counter must be exactly {cap}, got {total}"
