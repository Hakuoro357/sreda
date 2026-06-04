"""Generic usage_ledger: per-(tenant, metric, period_type, period_key) counter.

Phase 1 of free-tier-subscription plan
(`plans/free-tier-subscription-stalled-r7.md`).

Подход:
- Один table для всех metric'ов (не отдельный per-metric — один точечный
  schema, future metrics не требуют миграций).
- `period_type` = 'daily' | 'monthly'. `period_key` =
  'YYYY-MM-DD' для daily, 'YYYY-MM' для monthly. Время в Europe/Moscow
  (`services/usage_ledger.py` отвечает за period_key generation).
- `quota_snapshot` — copy of plan quota at write time, для audit
  (если plan quota меняется — historic rows не пересчитываются).
- UNIQUE (tenant_id, metric, period_type, period_key) — одна row per
  (tenant, metric, period). Updates атомарно через INSERT ... ON CONFLICT
  DO UPDATE WHERE quota check (см. `UsageLedgerService.try_consume`).

Sample rows:
  ('tenant_X', 'llm_turns', 'daily', '2026-05-07', 5, 10)
  ('tenant_X', 'llm_turns', 'monthly', '2026-05', 47, 200)
  ('tenant_X', 'voice_stt_seconds', 'monthly', '2026-05', 432, 1800)

NB: model defined for SQLAlchemy ORM scaffolding. Actual quota
enforcement (consume/refund) живёт в `services/usage_ledger.py`
(Phase 2) и использует `engine.begin()` для atomic SERIALIZABLE
transactions, не ORM session — поэтому ORM-доступ к этому table
ограничен read-only audit queries (admin dashboards и т.п.).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Metric keys (not enum — extensibility для future).
METRIC_LLM_TURNS = "llm_turns"
METRIC_VOICE_STT_SECONDS = "voice_stt_seconds"

# Period type keys.
PERIOD_DAILY = "daily"
PERIOD_MONTHLY = "monthly"


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "metric", "period_type", "period_key",
            name="uq_usage_ledger_period",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    period_type: Mapped[str] = mapped_column(String(8), nullable=False)
    period_key: Mapped[str] = mapped_column(String(10), nullable=False)
    used: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0"),
    )
    quota_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
