import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from sreda.api.deps import enforce_telegram_rate_limit
from sreda.config.settings import get_settings
from sreda.db.session import get_db_session
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.schemas.api import TelegramWebhookAccepted
from sreda.services import trace
from sreda.services.ack_messages import pick_ack
from sreda.services.inbound_messages import persist_telegram_inbound_event
from sreda.services.onboarding import ensure_telegram_user_bundle
from sreda.services.telegram_bot import handle_telegram_interaction

router = APIRouter(prefix="/webhooks/telegram", tags=["telegram"])
logger = logging.getLogger(__name__)


def _verify_telegram_secret_token(
    secret_token_header: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
) -> None:
    expected = get_settings().telegram_webhook_secret_token
    if not expected:
        # Dev-fallback: when the secret is not configured, accept all requests
        # to keep local/test setups working. Production deployments MUST set
        # SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN and match it at Telegram's
        # setWebhook call so every update carries this header.
        return
    if secret_token_header is None or not hmac.compare_digest(
        secret_token_header, expected
    ):
        logger.warning("telegram webhook rejected: secret token mismatch")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@router.post(
    "/{bot_key}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TelegramWebhookAccepted,
    # Rate-limit runs FIRST — it must reject hostile traffic before we
    # spend CPU on the ``hmac.compare_digest`` secret check, otherwise
    # an attacker without the secret can still tie up the event loop.
    dependencies=[
        Depends(enforce_telegram_rate_limit),
        Depends(_verify_telegram_secret_token),
    ],
)
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

        # Begin an end-to-end trace for this turn. Steps recorded
        # downstream (voice.transcribe, llm.iter.N, outbox.enqueued,
        # outbox.delivered) find this context via a ContextVar and
        # append to it. The block is emitted by the delivery worker
        # after the reply lands in Telegram (happy path); if the
        # handler never writes an outbox row, we emit here so the
        # trace isn't lost.
        trace_ctx = trace.start_trace(
            user_id=onboarding.user_id,
            tenant_id=onboarding.tenant_id,
            channel="telegram",
        )
        message = payload.get("message") if isinstance(payload, dict) else None
        message_type = "unknown"
        if isinstance(message, dict):
            if isinstance(message.get("voice"), dict):
                voice = message["voice"]
                message_type = "voice"
                trace.record(
                    "webhook.received",
                    type="voice",
                    voice_duration_s=voice.get("duration"),
                )
            elif message.get("text"):
                message_type = "text"
                trace.record("webhook.received", type="text")
            else:
                message_type = "other"
                trace.record("webhook.received", type="other")
        elif isinstance(payload, dict) and payload.get("callback_query"):
            message_type = "callback"
            trace.record("webhook.received", type="callback")
        else:
            trace.record("webhook.received", type="unknown")

        # Fast acknowledgement. A one-word reply ("работаю", "секунду",
        # ...) goes out immediately so the user sees the bot react while
        # the real turn is still crunching voice / LLM / outbox. Sent
        # DIRECTLY via the Telegram client — outbox adds ~1s worker-
        # poll latency which defeats the purpose. Skipped for button
        # taps (already feel instant) and new-user flow (they're
        # getting a welcome screen, not a question to ack).
        if (
            message_type in ("text", "voice")
            and not onboarding.is_new_user
        ):
            ack_text = pick_ack()
            with trace.step("ack.sent", phrase=ack_text) as _ack_meta:
                try:
                    await telegram_client.send_message(
                        chat_id=onboarding.chat_id,
                        text=ack_text,
                    )
                    _ack_meta["status"] = "ok"
                except TelegramDeliveryError as exc:
                    # UX sugar — don't fail the turn if ack can't be
                    # delivered. The real reply path is independent.
                    logger.warning("ack delivery failed: %s", exc)
                    _ack_meta["status"] = "failed"

        try:
            await handle_telegram_interaction(
                session,
                bot_key=bot_key,
                payload=payload,
                telegram_client=telegram_client,
                onboarding=onboarding,
                inbound_message_id=result.inbound_message_id,
            )
        except TelegramDeliveryError as exc:
            logger.warning("Telegram delivery failed during webhook handling: %s", exc)
        finally:
            # If the handler DID enqueue an outbox row, the delivery
            # worker will emit the block once it lands in Telegram.
            # Otherwise emit here so /help-style inline replies and
            # early-return paths still produce a trace record.
            if trace_ctx is not None and not any(
                e.step == "outbox.enqueued" for e in trace_ctx.events
            ):
                trace.emit_block(trace_ctx)
    return TelegramWebhookAccepted(ok=True, request_id=result.inbound_message_id)
