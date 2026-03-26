import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.db.session import get_db_session
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.schemas.api import TelegramWebhookAccepted
from sreda.services.inbound_messages import persist_telegram_inbound_event
from sreda.services.onboarding import ensure_telegram_user_bundle
from sreda.services.telegram_bot import handle_telegram_interaction

router = APIRouter(prefix="/webhooks/telegram", tags=["telegram"])
logger = logging.getLogger(__name__)


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
        try:
            await handle_telegram_interaction(
                session,
                bot_key=bot_key,
                payload=payload,
                telegram_client=telegram_client,
                onboarding=onboarding,
            )
        except TelegramDeliveryError as exc:
            logger.warning("Telegram delivery failed during webhook handling: %s", exc)
    return TelegramWebhookAccepted(ok=True, request_id=result.inbound_message_id)
