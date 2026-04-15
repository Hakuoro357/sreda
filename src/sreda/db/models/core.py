from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))


class TenantFeature(Base):
    __tablename__ = "tenant_features"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    telegram_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Assistant(Base):
    __tablename__ = "assistants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    job_type: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    # Required for retention cleanup (spec 41). Indexed because the cleanup
    # job filters by ``status IN (...) AND created_at < cutoff``.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    # Required for retention cleanup (spec 41): 30 days for sent,
    # 60 days for failed — all keyed off creation time since we don't
    # store a separate ``sent_at`` yet.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    # Deferred delivery (Phase 2). ``NULL`` means "send now". The outbox
    # delivery worker picks up rows where ``scheduled_at IS NULL OR
    # scheduled_at <= now``. Used by the quiet-hours enforcement to bump
    # non-urgent messages past the user's quiet window.
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Which skill produced this reply. ``NULL`` means "platform core"
    # (help/status/subscriptions). Used by quiet-hours enforcement to
    # look up per-skill ``notification_priority`` in
    # ``tenant_user_skill_configs``.
    feature_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # Recipient user (Phase 2d). The delivery worker uses this to resolve
    # per-user profile + per-skill priority. ``NULL`` means "broadcast
    # or system-level" and skips the user-scoped policy entirely.
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # ``True`` when this reply is a direct response to an inbound user
    # message (``action.inbound_message_id is not None``). Interactive
    # deliveries bypass quiet-hours — users get replies to their own
    # commands immediately, always.
    is_interactive: Mapped[bool] = mapped_column(Boolean, default=False)
    # Phase 5-lite: reason the message was dropped by decide_to_speak
    # or muted by skill config. Values: ``duplicate`` / ``throttle`` /
    # ``llm_filter`` (future) / ``muted`` / ``policy`` /NULL. Surfaces
    # in ``/stats`` so users see WHY the bot stayed silent.
    drop_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )


class SecureRecord(Base):
    __tablename__ = "secure_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    record_type: Mapped[str] = mapped_column(String(64), index=True)
    record_key: Mapped[str] = mapped_column(String(128), index=True)
    encrypted_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class InboundMessage(Base):
    __tablename__ = "inbound_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    channel_type: Mapped[str] = mapped_column(String(32), index=True)
    channel_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_key: Mapped[str] = mapped_column(String(64), index=True)
    external_update_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    sender_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    message_text_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    contains_sensitive_data: Mapped[bool] = mapped_column(Boolean, default=False)
    secure_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("secure_records.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="accepted", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
