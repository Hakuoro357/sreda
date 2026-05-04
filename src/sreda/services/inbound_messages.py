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


# ────────────────────────────────────────────────────────────────────
# MAX channel persistence (Phase 3 of MAX integration sprint)
# ────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class MaxInboundPersistResult:
    """Mirror TelegramInboundPersistResult для MAX."""

    inbound_message_id: str
    contains_sensitive_data: bool
    is_duplicate: bool = False
    update_type: str | None = None  # bot_started / message_created / ...


def persist_max_inbound_event(
    session: Session,
    *,
    bot_key: str,
    payload: dict,
) -> MaxInboundPersistResult:
    """Durable ingest of one MAX update.

    Probe (Phase 0) подтвердил формат:
    - ``update_type`` = ``bot_started`` | ``message_created``
    - Для ``message_created``: ``payload.message.body.{text, mid, seq}``,
      ``payload.message.sender.user_id``, ``payload.message.recipient.chat_id``
    - Для ``bot_started``: ``payload.user.user_id``, ``payload.chat_id``
    - Нет глобального ``update_id`` per event — используем ``body.mid``
      (для message_created) или synthetic key для остальных.

    Idempotent на duplicate ingest by ``external_update_id``: bot_started
    повторно (same chat) даст тот же synthetic key → no-op.
    """
    update_type = payload.get("update_type")
    chat_id = _extract_max_chat_id(payload)
    sender_user_id = _extract_max_sender_user_id(payload)
    message_text = _extract_max_message_text(payload)

    # Dedup key — mid из message body для message_created;
    # synthetic для bot_started (один на user×timestamp, переоткрытие
    # бота даст новый key).
    external_update_id = _extract_max_external_update_id(payload)

    user = None
    workspace = None
    tenant_id = None
    workspace_id = None
    user_id = None
    if sender_user_id is not None:
        from sreda.services.onboarding import find_user_by_max_account_id

        user = find_user_by_max_account_id(session, sender_user_id)
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

    # Idempotency check ДО создания secure_record (не платим encryption
    # на duplicate retries).
    if external_update_id is not None:
        existing = (
            session.query(InboundMessage)
            .filter(InboundMessage.external_update_id == external_update_id)
            .first()
        )
        if existing is not None:
            return MaxInboundPersistResult(
                inbound_message_id=existing.id,
                contains_sensitive_data=existing.contains_sensitive_data,
                is_duplicate=True,
                update_type=update_type,
            )

    secure_record = store_secure_json(
        session,
        record_type="max_webhook_raw",
        record_key=str(external_update_id or uuid4().hex),
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

    inbound = InboundMessage(
        id=f"in_{uuid4().hex[:24]}",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
        channel_type="max",
        channel_account_id=bot_key,
        bot_key=bot_key,
        external_update_id=external_update_id,
        sender_chat_id=str(chat_id) if chat_id is not None else None,
        message_text_sanitized=sanitized_text,
        contains_sensitive_data=contains_sensitive_data,
        secure_record_id=secure_record.id,
    )
    session.add(inbound)
    session.commit()

    return MaxInboundPersistResult(
        inbound_message_id=inbound.id,
        contains_sensitive_data=contains_sensitive_data,
        update_type=update_type,
    )


def _extract_max_external_update_id(payload: dict) -> str | None:
    """Per Phase 0 probe: MAX не имеет global ``update_id``.
    Используем ``body.mid`` для message_created; synthetic для остального.
    """
    update_type = payload.get("update_type")
    msg = payload.get("message")
    if isinstance(msg, dict):
        body = msg.get("body")
        if isinstance(body, dict):
            mid = body.get("mid")
            if mid:
                return str(mid)

    # Synthetic key для bot_started / других events:
    # ``<update_type>:<chat_id>:<timestamp>``
    chat_id = _extract_max_chat_id(payload)
    ts = payload.get("timestamp")
    if update_type and chat_id is not None and ts is not None:
        return f"max:{update_type}:{chat_id}:{ts}"
    return None


def _extract_max_chat_id(payload: dict) -> int | str | None:
    # bot_started: chat_id top-level. message_created: message.recipient.chat_id.
    if "chat_id" in payload:
        return payload.get("chat_id")
    msg = payload.get("message")
    if isinstance(msg, dict):
        recipient = msg.get("recipient")
        if isinstance(recipient, dict):
            return recipient.get("chat_id")
    return None


def _extract_max_sender_user_id(payload: dict) -> int | str | None:
    # bot_started: user.user_id. message_created: message.sender.user_id.
    user = payload.get("user")
    if isinstance(user, dict):
        return user.get("user_id")
    msg = payload.get("message")
    if isinstance(msg, dict):
        sender = msg.get("sender")
        if isinstance(sender, dict):
            return sender.get("user_id")
    return None


def _extract_max_message_text(payload: dict) -> str | None:
    msg = payload.get("message")
    if not isinstance(msg, dict):
        return None
    body = msg.get("body")
    if not isinstance(body, dict):
        return None
    text = body.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return None
