"""Housewife assistant skill — DB models.

Phase 1: ``family_reminders`` (scheduled proactive triggers).

Phase v1.2: ``family_members`` — structured rows per household member.
Powers shopping-list scaling (recipe servings × member count), menu
planning context enrichment, and a dedicated Mini App management page.
Name + notes encrypted at rest via EncryptedString. Replaces the
loose "состав семьи" kept only as ``AssistantMemory.content`` text.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FamilyReminder(Base):
    """One scheduled reminder for the user.

    Lifecycle:
      - Created by the housewife chat tool ``schedule_reminder`` with
        ``status='pending'`` and ``next_trigger_at`` set.
      - Picked up by ``HousewifeReminderWorker.process_pending`` when
        ``next_trigger_at <= now`` → outbox message + ``last_fired_at``.
      - If ``recurrence_rule`` is set, ``next_trigger_at`` is advanced
        via ``rrulestr`` and row stays ``pending``.
      - Otherwise row transitions to ``status='fired'`` and becomes
        invisible to the worker.
      - User can cancel from chat → ``status='cancelled'``.
    """

    __tablename__ = "family_reminders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    # Original trigger (what the user asked for). Kept for audit / display.
    trigger_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Optional RRULE (RFC 5545). Examples: ``FREQ=WEEKLY;BYDAY=TU;BYHOUR=16``,
    # ``FREQ=DAILY;BYHOUR=9``. ``NULL`` means one-shot.
    recurrence_rule: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # ``pending`` (worker-eligible) / ``fired`` (one-shot completed) /
    # ``cancelled`` (user cancelled).
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # The user phrase the LLM extracted the reminder from — useful for
    # debugging and for letting the user verify the interpretation.
    source_memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the worker should next fire this reminder. For one-shots equal
    # to ``trigger_at`` until fired; for recurring reminders advances past
    # each firing via rrulestr. ``NULL`` after a one-shot fires.
    next_trigger_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        # Composite index for the worker query: find all due rows cheaply
        # without a full scan. Status filter (= 'pending') is cheap enough
        # that we don't add it to the index.
        Index(
            "ix_family_reminders_tenant_next_trigger",
            "tenant_id",
            "next_trigger_at",
        ),
    )


FAMILY_ROLES = ("self", "spouse", "child", "parent", "other")


class FamilyMember(Base):
    """One row per member of the user's household.

    Minimal structured shape — we don't try to model relationships or
    full anamnesis. Just enough to: (a) compute list-scaling factor
    for the shopping generator, (b) enrich menu planning context
    ("учти аллергию Маши на горчицу"), (c) show a maintainable list
    in Mini App.

    Name and notes go through ``EncryptedString`` — these are identifying
    PII. Role / birth_year / age_hint are plaintext (used in filters
    and display, no leak beyond "there's a child aged 9").
    """

    __tablename__ = "family_members"
    __table_args__ = (
        Index("ix_family_members_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )

    name: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    # One of FAMILY_ROLES. ``self`` marks the user themselves — useful
    # so generation doesn't skip them when counting eaters.
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    # Birth year preferred (stable across time). age_hint is the
    # fallback when user only gave us "8 лет" without a year.
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    age_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    notes: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
