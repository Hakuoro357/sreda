"""Per-user profile + per-user per-skill config (Phase 2).

Distinct from Phase 0's ``TenantSkillConfig`` (tenant-wide skill overrides):
this is USER-level:

  * ``tenant_user_profiles``       — one row per (tenant, user): cross-skill
    identity, timezone, quiet hours, communication style, interest tags.
  * ``tenant_user_skill_configs``  — one row per (tenant, user, feature_key):
    notification priority, per-skill token budget, free-form skill params.

Audit is lightweight — each table keeps ``updated_by_source`` ("user_command"
| "agent_tool_confirmed" | "agent_tool_direct" | "system") and
``updated_by_user_id``. The full history is not kept; if we ever need it
we'll add a dedicated audit table.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantUserProfile(Base):
    __tablename__ = "tenant_user_profiles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", name="uq_tenant_user_profiles_tenant_user"
        ),
        Index("ix_tenant_user_profiles_timezone", "timezone"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Form of address chosen during onboarding: "ty" | "vy" | None.
    # NULL = ещё не выбрано (юзер не прошёл шаг 2 онбординга);
    # LLM/ack-фразы fallback'ат на нейтральную форму.
    address_form: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # IANA timezone string ("UTC", "Europe/Moscow"). Not validated here; the
    # handlers that write it use zoneinfo lookup to reject bad values at
    # the API boundary.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    # JSON array of windows: [{"from_hour": 0..23, "to_hour": 0..23,
    # "weekdays": [0..6]}]. from_hour == to_hour means "always quiet on
    # those weekdays"; to_hour < from_hour means "window crosses midnight".
    quiet_hours_json: Mapped[str] = mapped_column(Text, default="[]")
    # Enum: "terse" | "casual" | "formal". Default "casual".
    communication_style: Mapped[str] = mapped_column(String(16), default="casual")
    # JSON array of strings. Free-form tags the user cares about (used later
    # by proactive loop as relevance hints).
    interest_tags_json: Mapped[str] = mapped_column(Text, default="[]")

    # Lightweight audit — records WHO made the last change and by what
    # path. ``source`` values in sreda.db.repositories.user_profile.
    updated_by_source: Mapped[str] = mapped_column(String(32), default="user_command")
    updated_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Phase 5-lite: per-user throttle for proactive messages. When a
    # skill's proactive handler wants to push a reply, the policy
    # checks outbox for earlier proactive rows (same user, same
    # feature_key) within this window. If any exist, the new reply
    # is deferred rather than sent. 0 = no throttle (deliver every
    # proactive event immediately).
    proactive_throttle_minutes: Mapped[int] = mapped_column(Integer, default=30)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TenantUserProfileProposal(Base):
    """Agent-proposed profile change awaiting user confirmation (Phase 2e).

    Created when the agent calls its ``update_profile_field`` tool. The
    handler emits a Telegram message with inline ``Подтвердить`` /
    ``Отменить`` buttons whose ``callback_data`` embeds this row's id.
    Only on confirm do we apply the change to ``tenant_user_profiles``
    with ``updated_by_source='agent_tool_confirmed'``."""

    __tablename__ = "tenant_user_profile_proposals"
    __table_args__ = (
        Index("ix_tu_profile_proposals_status", "status"),
        Index("ix_tu_profile_proposals_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(64))
    proposed_value_json: Mapped[str] = mapped_column(Text)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "pending" | "confirmed" | "rejected" | "expired"
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TenantUserSkillConfig(Base):
    __tablename__ = "tenant_user_skill_configs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "feature_key",
            name="uq_tenant_user_skill_configs_tuf",
        ),
        Index("ix_tenant_user_skill_configs_feature", "feature_key"),
        Index("ix_tenant_user_skill_configs_priority", "notification_priority"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    feature_key: Mapped[str] = mapped_column(String(64))

    # Enum: "urgent" | "normal" | "low" | "mute". "urgent" bypasses quiet
    # hours. "mute" blocks all sends from this skill. Default "normal".
    notification_priority: Mapped[str] = mapped_column(String(16), default="normal")
    # Per-skill daily LLM token budget. 0 == unlimited.
    token_budget_daily: Mapped[int] = mapped_column(Integer, default=0)
    # Free-form JSON owned by the skill (e.g. EDS filter tags, housewife
    # onboarding state with family answers). Encrypted at rest —
    # skill_params often contain the user's structured personal data
    # (e.g. онбординг-ответы: "жена Екатерина, дети Николай/Никита/...").
    # Callers see plaintext JSON on read; writes accept plaintext.
    skill_params_json: Mapped[str] = mapped_column(
        EncryptedString(), default="{}"
    )

    updated_by_source: Mapped[str] = mapped_column(String(32), default="user_command")
    updated_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
