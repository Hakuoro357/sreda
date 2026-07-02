"""Snapshot storage for the admin overview dashboard (#292).

One row per snapshot key (currently just ``overview``). The payload is
a JSON blob computed by a background refresh loop in ``job_runner``;
the admin page only READS it (plus instant host metrics) — no network
calls or heavy aggregates on page load (owner requirement 2026-07-02).

Value, not PII: aggregates (counts, $ estimates, provider balances) —
no message content, no per-user rows beyond short tenant ids in the
error/slow lists that admins can already see elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AdminDashboardSnapshot(Base):
    __tablename__ = "admin_dashboard_snapshots"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
