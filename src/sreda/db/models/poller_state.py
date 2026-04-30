"""Long-poller state: offsets + heartbeats per channel.

Two-table split is deliberate:

- ``poller_offsets`` updates only when a real Telegram update is
  consumed (durable ingest path). It is **NOT** a liveness signal — in
  idle (a quiet 15-minute stretch) it stays stale even though the
  poller is healthy.

- ``poller_heartbeats`` updates after **every** ``getUpdates`` call,
  including empty long-polls (``200 []``) and network errors. It is
  the source of truth for monitor probes:
    * ``last_attempt_at`` = liveness (process is up, making requests)
    * ``last_ok_at`` = health (Telegram API is responding successfully)
  Distinguishing the two lets probes raise upstream-API problems as
  warning instead of mis-classifying them as «poller dead».

See plan ``mellow-discovering-conway.md`` (rounds 2-3 of code review).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class PollerOffset(Base):
    __tablename__ = "poller_offsets"

    channel: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class PollerHeartbeat(Base):
    __tablename__ = "poller_heartbeats"

    channel: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    last_ok_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
