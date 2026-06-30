"""Postgres-only concurrency tests for `try_consume_tavily` (#200 Фаза 1).

SQLite сериализует писателей (single-writer), поэтому гонку на границе
per-user=5 и global=950 он НЕ ловит — нужен реальный Postgres с
SERIALIZABLE + retry 40001. Эти тесты:

  1. 6 одновременных free-консьюмов (cap=5) → ровно 5 ALLOW + 1
     USER_EXHAUSTED (атомарный per-user не пробивается параллелью).
  2. Параллельные консьюмы на границе global=950 → НИ ОДИН не пробивает
     950 (SUM-резерв в той же SERIALIZABLE-txn ловит write-skew).

Запуск (как у message_queue concurrency):

  $ SREDA_TEST_POSTGRES_URL=postgresql://user:pw@localhost/sreda_test \
    SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1 \
    PYTHONPATH=src python -m pytest \
      tests/integration/test_web_search_quota_postgres_concurrency.py -v

Без обоих env-var модуль скипается (SQLite-юниты остаются зелёными).
"""

from __future__ import annotations

import os
import queue
import threading

import pytest
from sqlalchemy import create_engine, text

from sreda.db.models.web_search import WebSearchUsage
from sreda.services.web_search_usage import (
    QuotaDecision,
    try_consume_tavily,
)

_POSTGRES_URL = os.environ.get("SREDA_TEST_POSTGRES_URL")
_DESTRUCTIVE_OPT_IN = os.environ.get("SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN") == "1"

pytestmark = pytest.mark.skipif(
    not (_POSTGRES_URL and _DESTRUCTIVE_OPT_IN),
    reason=(
        "Postgres concurrency tests require BOTH SREDA_TEST_POSTGRES_URL "
        "and SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1 — they DROP and "
        "re-create web_search_usage. SQLite serializes writers so it "
        "cannot exercise the race; Postgres SERIALIZABLE is required."
    ),
)

_YM = "2026-06"


@pytest.fixture
def engine():
    """Per-test Postgres engine with a fresh web_search_usage table."""
    eng = create_engine(_POSTGRES_URL, echo=False, future=True)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS web_search_usage"))
        WebSearchUsage.__table__.create(conn)
    yield eng
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS web_search_usage"))
    eng.dispose()


def _run_concurrent(
    engine, n_threads: int, *, tenant_id, user_id_fn, per_user_cap, global_cap=950,
):
    """Spawn n_threads each calling try_consume_tavily; barrier-synced
    to maximize contention. Returns list of QuotaDecision."""
    barrier = threading.Barrier(n_threads)
    result_q: queue.Queue = queue.Queue()

    def runner(idx: int) -> None:
        try:
            barrier.wait(timeout=10.0)
            dec = try_consume_tavily(
                engine, tenant_id, user_id_fn(idx), per_user_cap, global_cap, _YM,
            )
            result_q.put(("ok", dec))
        except Exception as exc:  # noqa: BLE001
            result_q.put(("err", repr(exc)))

    threads = [
        threading.Thread(target=runner, args=(i,), daemon=True)
        for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    decisions = []
    while not result_q.empty():
        status, payload = result_q.get_nowait()
        assert status == "ok", f"thread errored: {payload!r}"
        decisions.append(payload)
    return decisions


def test_web_search_quota_concurrent_exactly_5(engine):
    """6 одновременных free-консьюмов одного юзера (cap=5) → ровно 5
    ALLOW + 1 USER_EXHAUSTED; счётчик в БД == 5."""
    decisions = _run_concurrent(
        engine, 6, tenant_id="t1", user_id_fn=lambda i: "u1", per_user_cap=5,
    )
    allow = sum(1 for d in decisions if d is QuotaDecision.ALLOW)
    user_exhausted = sum(1 for d in decisions if d is QuotaDecision.USER_EXHAUSTED)
    assert allow == 5, f"expected 5 ALLOW, got {allow}: {decisions}"
    assert user_exhausted == 1, f"expected 1 USER_EXHAUSTED, got {decisions}"

    with engine.begin() as conn:
        total = conn.execute(text(
            "SELECT tavily_calls FROM web_search_usage "
            "WHERE tenant_id='t1' AND user_id='u1' AND year_month=:ym"
        ), {"ym": _YM}).scalar()
    assert total == 5, f"counter should be exactly 5, got {total}"


def test_web_search_global_950_concurrent_blocks(engine):
    """На границе 950: пред-заполнить 949, затем 4 одновременных
    консьюма разных юзеров (cap=None) → ровно 1 ALLOW + 3
    GLOBAL_EXHAUSTED; SUM не пробивает 950."""
    # Pre-fill 949 (по 1 на юзера — per-user cap не мешает).
    with engine.begin() as conn:
        for i in range(949):
            conn.execute(text("""
                INSERT INTO web_search_usage
                    (id, tenant_id, user_id, year_month,
                     tavily_calls, fallback_calls, updated_at)
                VALUES (:id, 't1', :u, :ym, 1, 0, CURRENT_TIMESTAMP)
            """), {"id": f"pre_{i}", "u": f"pre{i}", "ym": _YM})

    decisions = _run_concurrent(
        engine, 4, tenant_id="t1",
        user_id_fn=lambda i: f"race{i}", per_user_cap=None,
    )
    allow = sum(1 for d in decisions if d is QuotaDecision.ALLOW)
    global_exhausted = sum(
        1 for d in decisions if d is QuotaDecision.GLOBAL_EXHAUSTED
    )
    assert allow == 1, f"expected exactly 1 ALLOW (950th), got {allow}: {decisions}"
    assert global_exhausted == 3, f"expected 3 GLOBAL_EXHAUSTED, got {decisions}"

    with engine.begin() as conn:
        total = conn.execute(text(
            "SELECT COALESCE(SUM(tavily_calls),0) FROM web_search_usage "
            "WHERE year_month=:ym"
        ), {"ym": _YM}).scalar()
    assert total == 950, f"global must not exceed 950, got {total}"
