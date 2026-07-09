"""Атомарная per-day квота fetch_url (#244, NO-REFUND).

Зеркало `try_consume_tavily` (web_search_usage.py), но:
* период — per-ДЕНЬ (`ymd`), не месяц;
* один счётчик `fetch_calls`, без global cap (у fetch нет метрированного API — лимит анти-злоупотребление);
* **НЕТ `release_*`**: расход безвозвратен. Резерв звать ТОЛЬКО после успешного pre-flight (socksio+proxy+
  TCP-probe PORT2) — тогда любой исход fetch (ProxyError/ConnectError/timeout/HTTP/success) уже оплачен, и
  эксплуатировать «бесплатные ретраи» нечем (нет refund-пути → нет дыры; снимает R3-medium/R5-high).

CAS — single-statement INSERT…ON CONFLICT DO UPDATE WHERE count<cap RETURNING (PG SERIALIZABLE + 40001-retry).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

logger = logging.getLogger(__name__)


def _current_ymd() -> str:
    """YYYY-MM-DD (UTC). Окно квоты — календарный день UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def try_consume_fetch_url(
    engine: Engine,
    tenant_id: str,
    user_id: str,
    per_day_cap: int | None,
    ymd: str | None = None,
) -> bool:
    """Атомарно зарезервировать ОДИН fetch_url за текущий день. True=зарезервировано, False=cap исчерпан.

    `per_day_cap is None` — без дневного лимита (paid/grandfathered): безусловный +1 (всё равно считаем для
    наблюдаемости/админки). `per_day_cap` задан (free) — INSERT-путь `WHERE 1<=cap`, UPDATE-путь
    `WHERE fetch_calls+1<=cap`; вернул строку → True, нет → False.

    NO-REFUND: компенсирующего decrement нет. Звать ТОЛЬКО после успешного pre-flight.

    СОБСТВЕННАЯ `engine.begin()`-txn (не session хода → нет autobegin-trap), PG SERIALIZABLE + retry 40001.
    """
    if not tenant_id or not user_id:
        return False  # без scope — fail-closed (anonymous отсечён выше по error:fetch_quota_unavailable)
    day = ymd or _current_ymd()
    is_pg = engine.dialect.name == "postgresql"
    # #331: изоляция через execution_options (до BEGIN), а не ручным SET внутри tx — конфликтует
    # с begin-событием #138 (set_config GUC). См. usage_ledger.try_consume.
    _engine = engine.execution_options(isolation_level="SERIALIZABLE") if is_pg else engine

    for attempt in range(3):
        try:
            with _engine.begin() as conn:
                row = _upsert_fetch_one(conn, tenant_id, user_id, day, per_day_cap)
                return row is not None
        except DBAPIError as exc:
            sqlstate = (
                getattr(exc.orig, "sqlstate", None)
                or getattr(exc.orig, "pgcode", None)
            )
            if sqlstate == "40001":  # serialization_failure
                backoff = 0.05 * (attempt + 1)
                logger.info(
                    "fetch_url consume serialization conflict, retry attempt=%d after %.0fms",
                    attempt + 1, backoff * 1000,
                )
                time.sleep(backoff)
                continue
            raise
    raise RuntimeError(
        "try_consume_fetch_url retried 3× — все serialization failures (40001)"
    )


def _upsert_fetch_one(conn, tenant_id: str, user_id: str, ymd: str, per_day_cap: int | None):
    """Один условный upsert +1. Returns row (truthy) или None.

    INSERT…ON CONFLICT DO UPDATE с RETURNING (PG ≥9.5, SQLite ≥3.35). `per_day_cap is None` → безусловный
    инкремент; иначе guard на INSERT-пути (`WHERE 1<=cap`) И UPDATE-пути (`fetch_calls+1<=cap`).
    """
    new_id = f"fuu_{uuid4().hex[:24]}"
    if per_day_cap is None:
        sql = text("""
            INSERT INTO fetch_url_usage
                (id, tenant_id, user_id, ymd, fetch_calls, updated_at)
            SELECT :id, :t, :u, :ymd, 1, CURRENT_TIMESTAMP
            ON CONFLICT (tenant_id, user_id, ymd)
            DO UPDATE SET
                fetch_calls = fetch_url_usage.fetch_calls + 1,
                updated_at = CURRENT_TIMESTAMP
            RETURNING fetch_calls
        """)
        params = {"id": new_id, "t": tenant_id, "u": user_id, "ymd": ymd}
    else:
        sql = text("""
            INSERT INTO fetch_url_usage
                (id, tenant_id, user_id, ymd, fetch_calls, updated_at)
            SELECT :id, :t, :u, :ymd, 1, CURRENT_TIMESTAMP
            WHERE 1 <= :cap
            ON CONFLICT (tenant_id, user_id, ymd)
            DO UPDATE SET
                fetch_calls = fetch_url_usage.fetch_calls + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE fetch_url_usage.fetch_calls + 1 <= :cap
            RETURNING fetch_calls
        """)
        params = {"id": new_id, "t": tenant_id, "u": user_id, "ymd": ymd, "cap": per_day_cap}
    return conn.execute(sql, params).first()
