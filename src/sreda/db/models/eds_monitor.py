from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class EDSAccount(Base):
    __tablename__ = "eds_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"), index=True)
    site_key: Mapped[str] = mapped_column(String(64))
    account_key: Mapped[str] = mapped_column(String(128), unique=True)
    label: Mapped[str] = mapped_column(String(255))
    login: Mapped[str] = mapped_column(String(255))


class EDSClaimState(Base):
    __tablename__ = "eds_claim_state"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    eds_account_id: Mapped[str] = mapped_column(ForeignKey("eds_accounts.id"), index=True)
    claim_id: Mapped[str] = mapped_column(String(64), index=True)
    fingerprint_hash: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_seen_changed: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_history_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_history_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_history_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_notified_event_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class EDSChangeEvent(Base):
    __tablename__ = "eds_change_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    eds_account_id: Mapped[str] = mapped_column(ForeignKey("eds_accounts.id"), index=True)
    claim_id: Mapped[str] = mapped_column(String(64), index=True)
    change_type: Mapped[str] = mapped_column(String(64), index=True)
    has_new_response: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_user_action: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class EDSDeliveryRecord(Base):
    __tablename__ = "eds_delivery_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    eds_account_id: Mapped[str] = mapped_column(ForeignKey("eds_accounts.id"), index=True)
    claim_id: Mapped[str] = mapped_column(String(64), index=True)
    recipient_chat_id: Mapped[str] = mapped_column(String(64), index=True)
    text_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_message_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
