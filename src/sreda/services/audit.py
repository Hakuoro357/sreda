"""Audit log service — запись важных событий в `audit_log` таблицу.

152-ФЗ Часть 2 (2026-04-28). Заменяет заглушку (которая просто возвращала
dict без записи) на реальный лог в БД.

Использование:

    from sreda.services.audit import audit_event

    audit_event(
        session,
        actor_type="admin",
        actor_id=hash_admin_token(token),
        action="admin.tenant.approve",
        resource_type="tenant",
        resource_id=tenant.id,
        metadata={"approved_by": "admin"},
    )

Best-effort: ошибки записи логируются но не пробрасываются — основной
flow не должен падать из-за audit log issues.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.audit import AuditLog


logger = logging.getLogger("sreda.audit")


_VALID_ACTOR_TYPES = frozenset({"admin", "user", "system"})


def audit_event(
    session: Session,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditLog | None:
    """Запись события в audit_log.

    Args:
        session: SQLAlchemy session.
        actor_type: "admin" | "user" | "system".
        actor_id: ID actor'а (для admin — hash токена, для user — user_id).
        action: канонический action key («admin.tenant.approve»).
        resource_type: тип объекта («tenant», «user», «message»).
        resource_id: ID объекта.
        metadata: dict без PII (только технические поля).
        commit: если True — commit'ит сразу. False для случаев когда
            вызывающий код управляет транзакцией сам.

    Returns:
        Созданный AuditLog row или None если запись не удалась.
    """
    if actor_type not in _VALID_ACTOR_TYPES:
        logger.error(
            "audit_event: invalid actor_type %r — skipping log", actor_type
        )
        return None

    try:
        row = AuditLog(
            id=f"audit_{uuid4().hex[:20]}",
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        if commit:
            session.commit()
        else:
            session.flush()
        return row
    except Exception:  # noqa: BLE001 — never break the calling flow
        logger.exception(
            "audit_event failed: action=%s resource=%s/%s",
            action, resource_type, resource_id,
        )
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def hash_admin_token(token: str) -> str:
    """Hash admin токена для безопасного хранения в audit_log.actor_id.
    Не используем сам токен в plaintext — иначе компрометация audit_log
    раскроет admin token.
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
