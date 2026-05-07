"""UsageLedgerService — atomic per-period quota consumption.

Phase 2 of free-tier-subscription plan
(`plans/free-tier-subscription-stalled-r7.md`).

Использование:
    ledger = UsageLedgerService(engine)
    msk_now = datetime.now(ZoneInfo("Europe/Moscow"))
    daily_key = msk_now.strftime("%Y-%m-%d")
    monthly_key = msk_now.strftime("%Y-%m")
    periods = [
        ("daily", daily_key, 20),
        ("monthly", monthly_key, 200),
    ]
    if not ledger.try_consume(tenant_id, "llm_turns", 1, periods):
        # exhausted in any period → reject without LLM call
        ...

Дизайн (per Codex review через 7 раундов):
- **Own engine.begin() connection**, не session.begin() — избегает
  SQLAlchemy autobegin conflict с handler's session.
- **Single CTE-based conditional UPSERT** per period — validates BOTH
  insert path (`WHERE :delta <= :quota`) AND update path
  (`WHERE used + delta <= quota`), atomic.
- **All-or-nothing across periods** — wraps two _try_consume_one calls
  в SERIALIZABLE tx; raises QuotaExhaustedError → tx rolls back.
- **Retry on serialization conflicts** (SQLSTATE 40001) — up to 3
  attempts с 50ms backoff. Catches `DBAPIError` (parent class
  обхватывает SQLAlchemy wrapping вариаций).
- **refund() owns own tx** — commits independently from caller's
  error path. Прямой воздействие: на STT failure caller refunds
  прежде чем re-raise; refund stuck даже если caller's outer tx
  rolls back.

Period semantics: `Europe/Moscow`. period_key format:
- daily: 'YYYY-MM-DD'
- monthly: 'YYYY-MM'
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError


logger = logging.getLogger(__name__)


_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# Phase 2 free-tier quota constants — single source of truth.
# Used by runtime/handlers.py (LLM gate) и services/{telegram_bot,
# max_inbound}.py (voice gates). Boris adjusts через RuntimeConfig
# в follow-up sprint (per Open Question #2 в plan); пока hardcoded.
SREDA_FREE_LLM_DAILY = 20
SREDA_FREE_LLM_MONTHLY = 200
SREDA_FREE_VOICE_SECONDS_DAILY = 5 * 60       # 5 minutes
SREDA_FREE_VOICE_SECONDS_MONTHLY = 30 * 60    # 30 minutes


def msk_period_keys(now: datetime | None = None) -> tuple[str, str]:
    """Return ('YYYY-MM-DD', 'YYYY-MM') в Europe/Moscow."""
    if now is None:
        now = datetime.now(_MOSCOW_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_MOSCOW_TZ)
    else:
        now = now.astimezone(_MOSCOW_TZ)
    return (now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"))


class QuotaExhaustedError(Exception):
    """Internal signal — used inside try_consume tx чтобы roll back и
    return False. Не propagate caller'у."""


@dataclass(frozen=True)
class PeriodSpec:
    period_type: str  # 'daily' | 'monthly'
    period_key: str
    quota: float | int | Decimal


class UsageLedgerService:
    """Atomic quota consume/refund. Uses engine-level transactions —
    НЕ session.begin() — чтобы избегать handler-session autobegin trap.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ---------------------------------------------------------- try_consume

    def try_consume(
        self,
        tenant_id: str,
        metric: str,
        amount: float | int | Decimal,
        periods: list[PeriodSpec | tuple[str, str, float | int]],
    ) -> bool:
        """Atomic check-and-increment across all periods. All-or-nothing.

        Args:
            tenant_id: target tenant.
            metric: 'llm_turns' | 'voice_stt_seconds' | etc.
            amount: amount to consume (positive int/float/Decimal).
            periods: list of (period_type, period_key, quota) tuples
                or PeriodSpec instances. All checked atomically.

        Returns:
            True если consume passed all periods. False если ANY
            period exceeds quota — никаких counters не incremented.

        Retry: SQLSTATE 40001 (serialization_failure) → up to 3
        attempts с 50ms exponential backoff.
        """
        periods_list = [self._normalize_period(p) for p in periods]

        is_pg = self.engine.dialect.name == "postgresql"
        for attempt in range(3):
            try:
                with self.engine.begin() as conn:
                    if is_pg:
                        conn.execute(text(
                            "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
                        ))
                    # SQLite: default journaling provides effective
                    # serialization для single-writer model — no SET needed.
                    for spec in periods_list:
                        if not self._try_consume_one(
                            conn, tenant_id, metric, amount, spec,
                        ):
                            raise QuotaExhaustedError(
                                f"{metric} {spec.period_type}={spec.period_key}"
                            )
                    return True
            except QuotaExhaustedError:
                return False
            except DBAPIError as exc:
                sqlstate = (
                    getattr(exc.orig, "sqlstate", None)
                    or getattr(exc.orig, "pgcode", None)
                )
                if sqlstate == "40001":  # serialization_failure
                    backoff = 0.05 * (attempt + 1)
                    logger.info(
                        "usage_ledger serialization conflict, "
                        "retry attempt=%d after %.0fms",
                        attempt + 1, backoff * 1000,
                    )
                    time.sleep(backoff)
                    continue
                raise
        raise RuntimeError(
            "Quota consume retried 3× — все serialization failures (SQLSTATE 40001)"
        )

    def _try_consume_one(
        self,
        conn,
        tenant_id: str,
        metric: str,
        amount: float | int | Decimal,
        spec: PeriodSpec,
    ) -> bool:
        """Atomic single-period consume через CTE-based UPSERT.

        Single SQL statement validates BOTH insert path
        (`WHERE :delta <= :quota`) AND update path
        (`WHERE used + delta <= quota`). Returns True если row
        inserted/updated, False если quota exceeded.

        Helper accepts conn explicitly — caller's `engine.begin()`
        block. NO reference к self.session — handler's session
        не affected.
        """
        # ON CONFLICT DO UPDATE с RETURNING — Postgres ≥ 9.5,
        # SQLite ≥ 3.35.0. CTE wrapper не нужен — RETURNING работает
        # напрямую (CTE с DML запрещён в SQLite).
        # NOW() supported in both PG (timezone-aware) and SQLite (UTC).
        sql = text("""
            INSERT INTO usage_ledger (tenant_id, metric, period_type,
                                       period_key, used, quota_snapshot,
                                       updated_at)
            SELECT :t, :m, :pt, :k, :delta, :quota, CURRENT_TIMESTAMP
            WHERE :delta <= :quota
            ON CONFLICT (tenant_id, metric, period_type, period_key)
            DO UPDATE SET
                used = usage_ledger.used + EXCLUDED.used,
                updated_at = CURRENT_TIMESTAMP
            WHERE usage_ledger.used + EXCLUDED.used <=
                  COALESCE(usage_ledger.quota_snapshot, EXCLUDED.quota_snapshot)
            RETURNING used
        """)
        row = conn.execute(sql, {
            "t": tenant_id, "m": metric,
            "pt": spec.period_type, "k": spec.period_key,
            "delta": float(amount), "quota": float(spec.quota),
        }).first()
        return row is not None

    # ---------------------------------------------------------- refund

    def refund(
        self,
        tenant_id: str,
        metric: str,
        amount: float | int | Decimal,
        periods: list[PeriodSpec | tuple[str, str, float | int]],
    ) -> None:
        """Decrement counters — own short-lived tx, commits independently.

        Used in voice flow: если STT failed после `try_consume()` succeeded,
        caller calls `refund()` чтобы отменить charge. Refund must commit
        BEFORE caller re-raises, иначе caller's outer error rolls back и
        юзер заряжен за failed voice.

        Floor at 0 (used = GREATEST(0, used - amount)) — defensive.
        """
        periods_list = [self._normalize_period(p) for p in periods]
        is_pg = self.engine.dialect.name == "postgresql"
        # SQLite не имеет GREATEST() — use MAX() instead.
        floor_fn = "GREATEST" if is_pg else "MAX"
        with self.engine.begin() as conn:
            for spec in periods_list:
                conn.execute(text(f"""
                    UPDATE usage_ledger
                    SET used = {floor_fn}(0, used - :amount),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = :t AND metric = :m
                      AND period_type = :pt AND period_key = :k
                """), {
                    "t": tenant_id, "m": metric,
                    "pt": spec.period_type, "k": spec.period_key,
                    "amount": float(amount),
                })

    # ---------------------------------------------------------- is_exhausted

    def is_exhausted(
        self,
        tenant_id: str,
        metric: str,
        spec: PeriodSpec | tuple[str, str, float | int],
    ) -> bool:
        """Read-only quota check. True если used >= quota для этого periode."""
        spec = self._normalize_period(spec)
        with self.engine.begin() as conn:
            row = conn.execute(text("""
                SELECT used FROM usage_ledger
                WHERE tenant_id = :t AND metric = :m
                  AND period_type = :pt AND period_key = :k
            """), {
                "t": tenant_id, "m": metric,
                "pt": spec.period_type, "k": spec.period_key,
            }).first()
        if row is None:
            return False
        return float(row.used) >= float(spec.quota)

    # ---------------------------------------------------------- helpers

    @staticmethod
    def _normalize_period(
        p: PeriodSpec | tuple[str, str, float | int],
    ) -> PeriodSpec:
        if isinstance(p, PeriodSpec):
            return p
        period_type, period_key, quota = p
        return PeriodSpec(period_type, period_key, quota)
