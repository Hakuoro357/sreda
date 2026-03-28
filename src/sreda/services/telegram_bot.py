from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.dispatcher import dispatch_telegram_action
from sreda.runtime.executor import ActionRuntimeService
from sreda.services.onboarding import TelegramOnboardingResult, build_welcome_message

logger = logging.getLogger(__name__)


async def handle_telegram_interaction(
    session: Session,
    *,
    bot_key: str,
    payload: dict,
    telegram_client: TelegramClient,
    onboarding: TelegramOnboardingResult,
    inbound_message_id: str | None = None,
) -> None:
    if onboarding.chat_id is None:
        return

    callback_query = payload.get("callback_query")
    if isinstance(callback_query, dict):
        await _handle_callback(
            session,
            telegram_client=telegram_client,
            callback_query=callback_query,
            onboarding=onboarding,
            bot_key=bot_key,
            payload=payload,
            inbound_message_id=inbound_message_id,
        )
        return

    if onboarding.is_new_user:
        text, reply_markup = build_welcome_message()
        await telegram_client.send_message(
            chat_id=onboarding.chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        return

    message_text = _extract_message_text(payload)
    if not message_text:
        return

    await _handle_command(
        session,
        telegram_client=telegram_client,
        bot_key=bot_key,
        payload=payload,
        onboarding=onboarding,
        inbound_message_id=inbound_message_id,
    )


async def _handle_callback(
    session: Session,
    *,
    telegram_client: TelegramClient,
    callback_query: dict,
    onboarding: TelegramOnboardingResult,
    bot_key: str,
    payload: dict,
    inbound_message_id: str | None,
) -> None:
    callback_id = callback_query.get("id")
    if callback_id:
        try:
            await telegram_client.answer_callback_query(str(callback_id), text="Готово")
        except TelegramDeliveryError as exc:
            logger.warning("Telegram callback acknowledgement failed: %s", exc)

    runtime_action = dispatch_telegram_action(
        payload=payload,
        bot_key=bot_key,
        onboarding=onboarding,
        inbound_message_id=inbound_message_id,
    )
    if runtime_action is None:
        return

    runtime = ActionRuntimeService(session, telegram_client=telegram_client)
    queued = runtime.enqueue_action(runtime_action)
    await runtime.process_job(queued.job_id)


async def _handle_command(
    session: Session,
    *,
    telegram_client: TelegramClient,
    bot_key: str,
    payload: dict,
    onboarding: TelegramOnboardingResult,
    inbound_message_id: str | None,
) -> None:
    runtime_action = dispatch_telegram_action(
        payload=payload,
        bot_key=bot_key,
        onboarding=onboarding,
        inbound_message_id=inbound_message_id,
    )
    if runtime_action is None:
        return

    runtime = ActionRuntimeService(session, telegram_client=telegram_client)
    queued = runtime.enqueue_action(runtime_action)
    await runtime.process_job(queued.job_id)


def _extract_message_text(payload: dict) -> str | None:
    for key in ("message", "edited_message"):
        message = payload.get(key)
        if not isinstance(message, dict):
            continue
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None
