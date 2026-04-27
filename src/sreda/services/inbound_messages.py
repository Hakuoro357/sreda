from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.core import InboundMessage, Workspace
from sreda.services.privacy_guard import get_default_privacy_guard
from sreda.services.secure_storage import store_secure_json


@dataclass(slots=True)
class TelegramInboundPersistResult:
    inbound_message_id: str
    contains_sensitive_data: bool
    # True when this payload matched an existing record by
    # ``external_update_id`` — Telegram long-poll / webhook retry
    # delivered the same update twice. Downstream handlers MUST
    # short-circuit on duplicates instead of firing a second chat
    # turn; the original request is already in flight (or done).
    is_duplicate: bool = False


def persist_telegram_inbound_event(
    session: Session,
    *,
    bot_key: str,
    payload: dict,
) -> TelegramInboundPersistResult:
    chat_id = _extract_chat_id(payload)
    message_text = _extract_message_text(payload)

    user = None
    workspace = None
    tenant_id = None
    workspace_id = None
    user_id = None
    if chat_id is not None:
        # Lookup by hash, не plaintext — 152-ФЗ обезличивание Часть 1.
        from sreda.services.onboarding import find_user_by_chat_id

        user = find_user_by_chat_id(session, chat_id)
    if user is not None:
        tenant_id = user.tenant_id
        user_id = user.id
        workspace = (
            session.query(Workspace)
            .filter(Workspace.tenant_id == tenant_id)
            .order_by(Workspace.id.asc())
            .first()
        )
        if workspace is not None:
            workspace_id = workspace.id

    secure_record = store_secure_json(
        session,
        record_type="telegram_webhook_raw",
        record_key=str(_extract_update_id(payload) or uuid4().hex),
        value=payload,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    session.flush()

    sanitized_text = None
    contains_sensitive_data = False
    if message_text is not None:
        sanitization = get_default_privacy_guard().sanitize_text(message_text)
        if sanitization is not None:
            sanitized_text = sanitization.sanitized_text
            contains_sensitive_data = sanitization.contains_sensitive_data

    update_id = _extract_update_id(payload)

    # M8: idempotency — if we already persisted an inbound message for
    # this ``update_id``, return the existing record instead of creating
    # a duplicate. Telegram may retry webhook delivery on network hiccups.
    if update_id is not None:
        existing = (
            session.query(InboundMessage)
            .filter(InboundMessage.external_update_id == update_id)
            .first()
        )
        if existing is not None:
            return TelegramInboundPersistResult(
                inbound_message_id=existing.id,
                contains_sensitive_data=existing.contains_sensitive_data,
                is_duplicate=True,
            )

    inbound = InboundMessage(
        id=f"in_{uuid4().hex[:24]}",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
        channel_type="telegram",
        channel_account_id=bot_key,
        bot_key=bot_key,
        external_update_id=update_id,
        sender_chat_id=chat_id,
        message_text_sanitized=sanitized_text,
        contains_sensitive_data=contains_sensitive_data,
        secure_record_id=secure_record.id,
    )
    session.add(inbound)
    session.commit()

    return TelegramInboundPersistResult(
        inbound_message_id=inbound.id,
        contains_sensitive_data=contains_sensitive_data,
    )


def _extract_update_id(payload: dict) -> str | None:
    value = payload.get("update_id")
    if value is None:
        return None
    return str(value)


def _extract_chat_id(payload: dict) -> str | None:
    message = _extract_message_container(payload)
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    return str(chat_id)


def _extract_message_text(payload: dict) -> str | None:
    message = _extract_message_container(payload)
    if not isinstance(message, dict):
        return None
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_message_container(payload: dict) -> dict | None:
    for key in ("message", "edited_message"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return None
