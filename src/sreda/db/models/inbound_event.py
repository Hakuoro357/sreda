"""Inbound events from skill ingestors (Phase 4).

Unified contract "skill → platform event" so skills don't reinvent
ingestion + dedup. An ingestor (cron job, webhook, push subscription)
creates rows in this table; a platform worker picks classified ones
up and feeds them to the skill's registered proactive handler, which
composes a user-facing reply.

Flow:
  1. Skill ingestor inserts row with status='new', relevance_score
     already filled (skill knows its own domain — platform doesn't
     LLM-classify by default).
  2. Optional classifier worker (disabled in MVP) could score rows
     without relevance_score; skipped for now.
  3. ``ProactiveEventWorker`` reads status='classified' rows whose
     score ≥ threshold, invokes skill's proactive handler, writes
     to outbox, marks consumed.

Dedup: UNIQUE(feature_key, external_event_key). Skill's ingestor
retries for the same event are no-ops.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InboundEvent(Base):
    __tablename__ = "inbound_events"
    __table_args__ = (
        UniqueConstraint(
            "feature_key",
            "external_event_key",
            name="uq_inbound_events_feature_external",
        ),
        Index("ix_inbound_events_status_created", "status", "created_at"),
        Index("ix_inbound_events_tenant_feature", "tenant_id", "feature_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    feature_key: Mapped[str] = mapped_column(String(64), index=True)

    # Skill-specific event type (e.g. "claim_updated", "daily_digest_due").
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    # Dedup key, unique per feature. For EDS: hash of claim_id + change_type.
    external_event_key: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")

    # Skill-assigned relevance (0..1). Skills with clear domain rules
    # write it directly; when the LLM-classifier path is enabled later,
    # it'll update classified rows here.
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    relevance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # "new" | "needs_classification" | "classified" | "consumed" | "skipped"
    #   * "new"                   — below score threshold, no further action
    #   * "needs_classification"  — skill didn't score; waiting for the
    #                               future LLM-classifier worker (see
    #                               ``sreda.services.relevance_classifier``
    #                               hook — currently stub, enabled when
    #                               ``settings.mimo_classifier_model`` set).
    #   * "classified"            — ready for proactive worker
    #   * "consumed"              — proactive handler ran successfully
    #   * "skipped"               — no handler / quota exhausted / muted
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    status_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    classified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
