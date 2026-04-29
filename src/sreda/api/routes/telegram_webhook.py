import asyncio
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from sreda.api.deps import enforce_telegram_rate_limit
from sreda.config.settings import get_settings
from sreda.db.models.core import Tenant
from sreda.db.session import get_db_session
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.schemas.api import TelegramWebhookAccepted
from sreda.services import trace
from sreda.services.ack_messages import pick_ack
from sreda.services.inbound_messages import persist_telegram_inbound_event
from sreda.services.onboarding import ensure_telegram_user_bundle
from sreda.services.telegram_bot import handle_telegram_interaction

# Pending-bot scripted-engine с демо-рассказом живёт в
# ``sreda.services.pending_bot``. В этом файле мы только вызываем его
# match() и отправляем результат в Telegram (см. approval-gate ниже).

router = APIRouter(prefix="/webhooks/telegram", tags=["telegram"])
logger = logging.getLogger(__name__)


async def _fire_and_forget_ack(
    client: TelegramClient, chat_id: str, text: str,
) -> None:
    """Send a one-word ack reply concurrently with main turn processing.

    Used as ``asyncio.create_task`` target so ``await client.send_message``
    doesn't block voice.download / text llm. Uses parent's TraceContext
    (ContextVar copied into child task) — the ``ack.sent`` event lands
    in the same trace block as the rest of the turn. Failures are
    logged but don't propagate (UX sugar — don't fail the turn if ack
    can't be delivered)."""
    with trace.step("ack.sent", phrase=text) as meta:
        try:
            await client.send_message(chat_id=chat_id, text=text)
            meta["status"] = "ok"
        except TelegramDeliveryError as exc:
            logger.warning("ack delivery failed: %s", exc)
            meta["status"] = "failed"
        except Exception as exc:  # noqa: BLE001
            # Defensive: ack must NEVER take down a turn.
            logger.exception("ack task crashed: %s", exc)
            meta["status"] = "crashed"


async def _fire_chat_action_typing(
    client: TelegramClient, chat_id: str,
) -> None:
    """Fire-and-forget Telegram «печатает…» индикатор на webhook entry.

    2026-04-29: одноразовый sendChatAction(typing). Telegram держит
    индикатор ~5с, потом гасит сам. Параллельно в той же задаче
    летит ack-message — когда он доходит до юзера, индикатор сменяется
    реальным текстом (Telegram гасит typing на любом message от бота).

    Никаких keep-alive циклов — индикатор должен исчезнуть сам через
    5с в худшем сценарии. Если ack успел придти раньше (обычно ~80-200мс
    с warm pool), typing вообще не успевает быть виден визуально как
    отдельная фаза — юзер видит typing→ack плавно. На холодном pool
    sendChatAction сам прогревает TLS-connection => ack использует уже
    warm connection.

    Failures DEBUG-level: chat action — UX sugar, не correctness."""
    try:
        await client.send_chat_action(chat_id=chat_id, action="typing")
    except TelegramDeliveryError as exc:
        logger.debug("chat_action delivery failed status=%s", exc.status_code)
    except Exception:  # noqa: BLE001
        logger.debug("chat_action task crashed", exc_info=True)


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

    # Long-poll / webhook retry short-circuit. Same update_id → a
    # turn is already in-flight (or finished) for this input. Firing
    # a second chat turn causes the double-reply bug observed
    # 2026-04-22: long-running turn failed to ack in time, Telegram
    # re-delivered the same voice, both turns ran to completion, user
    # got duplicate replies with slightly different wording.
    if result.is_duplicate:
        logger.info(
            "telegram webhook: duplicate update_id %s for bot %s — "
            "skipping second chat turn",
            payload.get("update_id"), bot_key,
        )
        return TelegramWebhookAccepted(
            ok=True, request_id=result.inbound_message_id,
        )

    # Approval gate (2026-04-23 MVP). New tenants created via /start
    # land with ``approved_at IS NULL`` — webhook either sends the
    # "заявка принята" reply once (on the very first call from a new
    # user) or silent-drops everything else until an admin clicks
    # "Одобрить" in /admin/users. Approved tenants fall through to
    # the normal ack + chat-turn flow below.
    settings = get_settings()
    tenant = session.get(Tenant, onboarding.tenant_id)
    is_approved = tenant is not None and tenant.approved_at is not None
    if not is_approved:
        # Часть B плана v2: вместо silent-drop'а гоняем юзера в
        # scripted-engine ``pending_bot`` с 6 ветками демо-рассказа.
        # Он вернёт (text, buttons) — отправляем обычным inline-keyboard.
        if settings.telegram_bot_token and onboarding.chat_id:
            from sreda.services import pending_bot

            # Что юзер прислал в этом апдейте — текст, callback или голос.
            message = payload.get("message") if isinstance(payload, dict) else None
            callback_query = (
                payload.get("callback_query")
                if isinstance(payload, dict) else None
            )
            input_text: str | None = None
            is_callback = False
            if isinstance(callback_query, dict):
                cb_data = str(callback_query.get("data") or "")
                # Pending-кнопки имеют префикс ``pb:``; остальное
                # (например btn_reply:) — это чужие колбэки, которые
                # в pending-состоянии бессмысленны; всё равно отдаём
                # welcome. Кнопки reminder'ов (rem_done/rem_snooze) тут
                # невозможны — до approval мы не создавали reminders.
                if pending_bot.is_pending_callback(cb_data):
                    input_text = cb_data
                    is_callback = True
                else:
                    input_text = None
            elif isinstance(message, dict) and message.get("text"):
                input_text = str(message.get("text"))
            # Для voice в pending — не идём в STT (затратно), показываем
            # welcome. После approval голос заработает как обычно.
            reply = pending_bot.match(input_text, is_callback=is_callback)
            pending_client = TelegramClient(settings.telegram_bot_token)

            # Если это callback — сразу ответим тостом, чтобы Telegram
            # не крутил «loading» на кнопке.
            cb_id = (
                str(callback_query.get("id") or "")
                if isinstance(callback_query, dict) else ""
            )
            if cb_id:
                try:
                    await pending_client.answer_callback_query(
                        cb_id, text=""
                    )
                except TelegramDeliveryError:
                    pass

            try:
                await pending_client.send_message(
                    chat_id=onboarding.chat_id,
                    text=reply.text,
                    reply_markup=pending_bot.build_inline_keyboard(reply),
                )
            except TelegramDeliveryError as exc:
                logger.warning(
                    "pending_bot reply failed tenant=%s: %s",
                    onboarding.tenant_id, exc,
                )
        else:
            logger.info(
                "pending tenant %s — no bot token/chat_id, drop (update_id=%s)",
                onboarding.tenant_id, payload.get("update_id"),
            )
        return TelegramWebhookAccepted(
            ok=True, request_id=result.inbound_message_id,
        )

    # 2026-04-27 simplified: state-machine «имя + ты/вы» удалена.
    # После approval-gate юзер сразу попадает в обычный chat-flow.
    # Имя извлекается LLM-tool'ом `update_profile_field` во время
    # естественного диалога — webhook ничего не перехватывает.

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
        #
        # 2026-04-29 (Phase C optimization): ack идёт через
        # `asyncio.create_task` — fire-and-forget параллельно с
        # voice.download/transcribe. Раньше `await` блокировал на
        # ~425мс (через SOCKS5 RTT). Теперь download стартует
        # одновременно с ack send, экономит этот штраф из user-perceived
        # latency. ContextVar (TraceContext) копируется в child task
        # автоматически, так что trace.step внутри ack корректно пишет
        # в текущий trace блок. В конце handler'а ждём `ack_task`
        # с таймаутом 2с — обычно он уже завершён (sendMessage ~400мс
        # vs voice flow ~10с), но явное ожидание гарантирует что trace
        # блок включит ack.sent перед emit_block.
        ack_task: asyncio.Task | None = None
        if (
            message_type in ("text", "voice")
            and not onboarding.is_new_user
        ):
            # 2026-04-29: chat_action «печатает…» fire-and-forget.
            # Самый дешёвый UX-сигнал юзеру что бот реагирует. Идёт
            # параллельно с ack — обычно ack догоняет за 80-200мс и
            # сменяет индикатор на текст. На холодном pool'е
            # sendChatAction служит pool-warmer'ом для последующего
            # ack. Не trace'ится (DEBUG-level only on errors).
            asyncio.create_task(
                _fire_chat_action_typing(
                    telegram_client, onboarding.chat_id,
                ),
                name=f"chat_action:{onboarding.chat_id}",
            )
            ack_text = pick_ack()
            ack_task = asyncio.create_task(
                _fire_and_forget_ack(
                    telegram_client, onboarding.chat_id, ack_text,
                ),
                name=f"ack:{onboarding.chat_id}",
            )

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
            # Wait briefly for ack task to complete so ``ack.sent`` event
            # is recorded in trace block before emit. Voice flow takes
            # ~10с, ack ~400мс — task обычно already done by here.
            # Cap at 2с safety so a stuck ack doesn't delay the response.
            if ack_task is not None and not ack_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(ack_task), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "ack task still pending after main turn — abandoning",
                    )

            # If the handler DID enqueue an outbox row, the delivery
            # worker will emit the block once it lands in Telegram.
            # Otherwise emit here so /help-style inline replies and
            # early-return paths still produce a trace record.
            if trace_ctx is not None and not any(
                e.step == "outbox.enqueued" for e in trace_ctx.events
            ):
                trace.emit_block(trace_ctx)
    return TelegramWebhookAccepted(ok=True, request_id=result.inbound_message_id)
