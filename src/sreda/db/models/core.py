from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 152-ФЗ Часть 2 (2026-04-28): tenant.name содержит Telegram
    # first/last name юзера — это PII. Шифруется через EncryptedString;
    # в дампе БД лежит base64-шифр. ORM прозрачно расшифровывает на read.
    name: Mapped[str] = mapped_column(EncryptedString())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    # Approval gate (2026-04-23, MVP-костыль до подписок). NULL = заявка
    # принята, но ещё не одобрена модератором; сообщения silent-drop'ятся
    # в telegram_webhook. Одобрение — в админке /admin/users. Существующие
    # тенанты помечены NOW() миграцией при накатывании колонки, так что
    # живые пользователи не ломаются.
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
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
    # Plaintext Telegram chat_id, зашифрован через EncryptedString
    # (152-ФЗ обезличивание Часть 1, 2026-04-27). В дампе БД лежит
    # base64-шифр, не PII. Worker'ы получают plaintext через ORM read
    # (TypeDecorator расшифровывает) — нужно для вызова `sendMessage`.
    # Lookup по chat_id идёт через `tg_account_hash` (HMAC-SHA256),
    # см. `services/tg_account_hash.py` + `find_user_by_chat_id`.
    telegram_account_id: Mapped[str | None] = mapped_column(
        EncryptedString(), nullable=True,
    )
    # Hash от plaintext chat_id для O(1) lookup'а без расшифровки
    # всех записей. Backfill миграцией 0027. Unique — один tg-аккаунт
    # = один user. None для legacy/seed юзеров без telegram_account_id.
    tg_account_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True,
    )


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
    # 152-ФЗ Часть 2: payload_json может содержать task title / args
    # с PII. Шифруется через EncryptedString.
    payload_json: Mapped[str] = mapped_column(EncryptedString(), default="{}")
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
    # 152-ФЗ Часть 2: payload_json содержит сгенерированный LLM текст
    # ответа бота — это контент переписки. Шифруется EncryptedString.
    payload_json: Mapped[str] = mapped_column(EncryptedString())
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
    # 152-ФЗ Часть 2: содержит входящие сообщения юзера (после
    # privacy_guard санитизации). Это контент переписки → шифруем.
    message_text_sanitized: Mapped[str | None] = mapped_column(
        EncryptedString(), nullable=True
    )
    contains_sensitive_data: Mapped[bool] = mapped_column(Boolean, default=False)
    secure_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("secure_records.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="accepted", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


# 152-ФЗ обезличивание Часть 1 (2026-04-27): автоматически вычисляем
# `tg_account_hash` каждый раз, когда выставляется `telegram_account_id`.
# Это снимает с вызывающего кода (онбординг, тесты, seed) обязанность
# помнить про вторую колонку — достаточно записать chat_id, hash
# заполнится сам.
#
# Если salt не сконфигурирован (`SREDA_TG_ACCOUNT_SALT`), листенер
# падает RuntimeError — но это правильно: иначе lookup по hash будет
# всегда возвращать None и юзер «потеряется». Тесты подсовывают salt
# через conftest.
@event.listens_for(User.telegram_account_id, "set", retval=False)
def _user_telegram_account_id_set(  # noqa: ANN001 — SQLAlchemy event signature
    target, value, oldvalue, initiator,  # noqa: ARG001
):
    if value is None or value == "":
        target.tg_account_hash = None
        return
    if isinstance(value, str) and not value.strip():
        target.tg_account_hash = None
        return
    # Lazy import — services.tg_account_hash тянет settings,
    # которые при импорте models создают цикл.
    from sreda.services.tg_account_hash import hash_tg_account

    target.tg_account_hash = hash_tg_account(value)
