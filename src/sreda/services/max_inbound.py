"""MAX inbound channel — durable ingest + downstream dispatch.

Phase 3 of MAX integration. Mirror ``services.telegram_inbound`` shape:
``handle_max_update`` (entry point) делает durable ingest через
``persist_max_inbound_event`` и шедулит обработку в background.

Channel linking note (R3 design pivot, 2026-05-04 PM):
Изначально планировалось перехватывать `bot_started` events с
``start_param=lnk_<token>`` для channel linking. Но Phase 0 probe
показал что в MAX `?startapp=` НЕ доставляется боту (без зарегистрированной
mini-app), а доставляется в mini-app через initData.start_param.
Соответственно channel linking flow смещён в mini-app endpoints
(``api/routes/miniapp_channel_link.py``), а ``handle_max_update``
не делает специальную обработку start_param. Bot_started → нормальный
welcome flow, message_created → нормальный AI flow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sreda.config.settings import get_settings
from sreda.db.models.core import Tenant
from sreda.db.session import get_session_factory
from sreda.services.inbound_messages import persist_max_inbound_event
from sreda.services.onboarding import ensure_max_user_bundle

if TYPE_CHECKING:
    from fastapi import BackgroundTasks


logger = logging.getLogger(__name__)


async def handle_max_update(
    payload: dict,
    *,
    bot_key: str = "sreda_max",
    background_tasks: "BackgroundTasks | None" = None,
) -> str:
    """Durable ingest of one MAX update.

    Returns ``inbound_message_id`` once row committed.

    Idempotent on duplicate ingest (same body.mid OR synthetic key) —
    second call для same update — no-op (row already exists).

    Architecture:
    1. Извлечь user/chat IDs из update — ``ensure_max_user_bundle``
       создаёт tenant/user если новый юзер.
    2. ``persist_max_inbound_event`` — durable INSERT в ``inbound_messages``,
       commit. С этого момента poller / webhook handler могут безопасно
       advance offset.
    3. Если duplicate — return early.
    4. Если tenant pending approval — log + ignored (pending_bot для MAX
       пока не реализован, чёткий MVP fail-soft).
    5. Если approved — schedule ``_process_approved_max_turn`` detached
       (BackgroundTasks для webhook / asyncio.create_task для poller-like).
    """
    settings = get_settings()
    SessionLocal = get_session_factory()
    update_type = payload.get("update_type")

    # Извлекаем sender user_id и chat_id для onboarding.
    from sreda.services.inbound_messages import (
        _extract_max_chat_id, _extract_max_sender_user_id,
    )
    sender_user_id = _extract_max_sender_user_id(payload)
    chat_id = _extract_max_chat_id(payload)

    if sender_user_id is None:
        # Странный update без sender — log + skip (но не raise; webhook
        # должен 200-OK даже на garbage payload).
        logger.warning(
            "max inbound: cannot extract sender user_id from %s update",
            update_type,
        )
        return ""

    with SessionLocal() as session:
        # Sender display name (best-effort).
        display_name = _extract_max_display_name(payload)
        onboarding = ensure_max_user_bundle(
            session,
            max_account_id=sender_user_id,
            max_chat_id=chat_id,
            display_name=display_name,
        )

        result = persist_max_inbound_event(
            session, bot_key=bot_key, payload=payload,
        )

        if result.is_duplicate:
            logger.info(
                "max inbound: duplicate update %s for bot %s — no-op",
                result.inbound_message_id, bot_key,
            )
            return result.inbound_message_id

        tenant = session.get(Tenant, onboarding.tenant_id)
        is_approved = tenant is not None and tenant.approved_at is not None

        if not is_approved:
            # MVP: pending-flow для MAX = silent skip + log. Когда
            # pending_bot будет channel-agnostic, добавим welcome flow.
            logger.info(
                "max inbound: pending tenant %s — drop (update_type=%s)",
                onboarding.tenant_id, update_type,
            )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        if not (settings.max_bot_token and onboarding.max_chat_id):
            logger.info(
                "approved tenant %s — no MAX token/chat_id, drop "
                "(inbound_id=%s)",
                onboarding.tenant_id, result.inbound_message_id,
            )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        inbound_message_id = result.inbound_message_id

    # Detached approved turn. Phase 6 (outbox routing) обеспечит что
    # ответ уйдёт в MAX channel, не TG.
    if background_tasks is not None:
        background_tasks.add_task(
            _process_approved_max_turn,
            bot_key=bot_key,
            payload=payload,
            onboarding=onboarding,
            inbound_message_id=inbound_message_id,
        )
    else:
        asyncio.create_task(
            _process_approved_max_turn(
                bot_key=bot_key,
                payload=payload,
                onboarding=onboarding,
                inbound_message_id=inbound_message_id,
            ),
            name=f"max_approved_turn:{onboarding.tenant_id}:{inbound_message_id}",
        )
    return inbound_message_id


async def _process_approved_max_turn(
    *,
    bot_key: str,
    payload: dict,
    onboarding,  # MaxOnboardingResult
    inbound_message_id: str,
) -> None:
    """Run the heavy approved-tenant turn for a MAX update.

    Phase 3 placeholder — пока что просто log + mark processed.
    Phase 6 свяжет с conversation graph (тот же graph что для TG).

    Future: вызывать ``runtime.handlers.execute_conversation_chat`` channel-agnostic
    с ``channel_type="max"`` и delivery routing через outbox c
    ``max_chat_id`` recipient.
    """
    logger.info(
        "max approved turn placeholder: tenant=%s inbound=%s update_type=%s",
        onboarding.tenant_id, inbound_message_id, payload.get("update_type"),
    )
    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        _set_processing_status(session, inbound_message_id, "processed")


def _set_processing_status(session, inbound_message_id: str, status: str) -> None:
    """Update inbound_messages.processing_status + commit."""
    from sreda.db.models.core import InboundMessage

    row = session.get(InboundMessage, inbound_message_id)
    if row is not None:
        row.processing_status = status
        session.commit()


def _extract_max_display_name(payload: dict) -> str | None:
    """Best-effort first/last/name из MAX payload (bot_started ИЛИ
    message_created)."""
    user = payload.get("user")
    if isinstance(user, dict):
        for key in ("name", "first_name"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    msg = payload.get("message")
    if isinstance(msg, dict):
        sender = msg.get("sender")
        if isinstance(sender, dict):
            for key in ("name", "first_name"):
                value = sender.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None
