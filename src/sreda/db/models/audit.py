"""Audit log model — кто, что, когда сделал с системой.

152-ФЗ Часть 2 (2026-04-28): требование «логирование действий с
ПДн». Логируем важные admin / user actions, не каждый decrypt
(шумно). Для compliance retention этой таблицы должен быть отдельный
— минимум 1 год (см. retention_cleanup TTL'ы).

Что логируем:
  * admin.tenant.approve / .reset — действия модератора
  * admin.users.viewed             — заход в админку (опционально)
  * user.self_delete.requested / .completed — юзер удаляет аккаунт
  * user.privacy_consent.given     — даст согласие на ПДн (когда сайт)

Что НЕ логируем:
  * каждый decrypt в hot path — шум, миллионы записей
  * каждый chat turn — шум, не compliance-relevant
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_action_created", "action", "created_at"),
        Index("ix_audit_log_actor", "actor_type", "actor_id"),
        Index("ix_audit_log_resource", "resource_type", "resource_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # "admin" | "user" | "system" — кто инициировал.
    actor_type: Mapped[str] = mapped_column(String(16))
    # admin: SHA-256 hash от admin token (не сам токен — security).
    # user: tenant_id или user_id юзера. system: "system".
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Канонический action key, точкой разделённый: "admin.tenant.approve",
    # "user.self_delete.requested", "user.privacy_consent.given".
    action: Mapped[str] = mapped_column(String(64), index=True)
    # Тип объекта над которым действие. "tenant", "user", "memory", None.
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # ID объекта (tenant_id, message_id и т.д.). None если действие
    # не привязано к конкретному ресурсу.
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Произвольная metadata JSON-сериализованная. БЕЗ PII (только
    # технические поля: status_before, status_after, request_id и т.п.).
    # Не EncryptedString — здесь plaintext по дизайну (compliance-проверки
    # должны работать без ключа).
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
