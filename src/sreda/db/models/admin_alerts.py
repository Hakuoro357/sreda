"""R-28: AdminAlertSeen model for dedup of admin alerts.

Persistent dedup table keyed by stable ``dedupe_key`` (e.g.
``"llm_fallback:BadRequestError:housewife_assistant"``). Service
``services/admin_alerts.py`` checks ``last_sent_at`` против rate-limit
window перед sending Telegram message.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class AdminAlertSeen(Base):
    __tablename__ = "admin_alerts_seen"

    dedupe_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    occurrence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
