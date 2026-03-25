from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.db.session import get_db_session
from sreda.integrations.telegram.client import TelegramClient
from sreda.schemas.api import TelegramWebhookAccepted
from sreda.services.inbound_messages import persist_telegram_inbound_event
from sreda.services.onboarding import (
    CONNECT_EDS_CALLBACK,
    build_connect_eds_message,
    build_welcome_message,
    ensure_telegram_user_bundle,
)

router = APIRouter(prefix="/webhooks/telegram", tags=["telegram"])


@router.post("/{bot_key}", status_code=status.HTTP_202_ACCEPTED, response_model=TelegramWebhookAccepted)
async def telegram_webhook(
    bot_key: str,
    payload: dict,
    session: Session = Depends(get_db_session),
) -> TelegramWebhookAccepted:
    onboarding = ensure_telegram_user_bundle(session, payload)
    result = persist_telegram_inbound_event(
        session,
        bot_key=bot_key,
        payload=payload,
    )
    settings = get_settings()
    if settings.telegram_bot_token and onboarding.chat_id:
        telegram_client = TelegramClient(settings.telegram_bot_token)
        callback_query = payload.get("callback_query")
        if (
            isinstance(callback_query, dict)
            and callback_query.get("data") == CONNECT_EDS_CALLBACK
            and callback_query.get("id")
        ):
            await telegram_client.answer_callback_query(
                str(callback_query["id"]),
                text="Запрос на подключение EDS принят",
            )
            await telegram_client.send_message(
                chat_id=onboarding.chat_id,
                text=build_connect_eds_message(),
            )
        elif onboarding.is_new_user:
            text, reply_markup = build_welcome_message()
            await telegram_client.send_message(
                chat_id=onboarding.chat_id,
                text=text,
                reply_markup=reply_markup,
            )
    return TelegramWebhookAccepted(ok=True, request_id=result.inbound_message_id)
