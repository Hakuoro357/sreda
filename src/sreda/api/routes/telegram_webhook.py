import asyncio
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from sreda.api.deps import enforce_telegram_rate_limit
from sreda.config.settings import get_settings
from sreda.db.models.core import Tenant
from sreda.db.session import get_db_session, get_session_factory
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
) -> int | None:
    """Send a one-word ack reply concurrently with main turn processing.

    Returns Telegram message_id of the ack (None on failure). Used by
    webhook handler чтобы потом вызвать `delete_message` после
    доставки реального reply'я — clean-chat UX (одно сообщение в
    чате на turn вместо двух).

    Используется как ``asyncio.create_task`` target — `await
    client.send_message` не блокирует voice.download/transcribe.
    ContextVar (TraceContext) копируется в child task автоматически,
    `ack.sent` event попадает в trace-блок текущего turn'а.
    Failures swallowed: ack — UX sugar, не correctness-critical."""
    with trace.step("ack.sent", phrase=text) as meta:
        try:
            response = await client.send_message(chat_id=chat_id, text=text)
            meta["status"] = "ok"
            result = response.get("result") or {}
            mid = result.get("message_id")
            return int(mid) if isinstance(mid, int) else None
        except TelegramDeliveryError as exc:
            logger.warning("ack delivery failed: %s", exc)
            meta["status"] = "failed"
            return None
        except Exception as exc:  # noqa: BLE001
            logger.exception("ack task crashed: %s", exc)
            meta["status"] = "crashed"
            return None


async def _delete_ack_after_reply(
    client: TelegramClient, chat_id: str, message_id: int,
) -> None:
    """Удаляет ack-сообщение после доставки реального reply'я.

    2026-04-29: clean-chat UX. Раньше юзер видел в чате 2 сообщения
    на turn: ack «🔍 Смотрю…» + reply. Теперь ack удаляется после
    того как reply дошёл до юзера → остаётся одно сообщение реального
    ответа. Ack виден только пока bot crunch'ит (200мс — 15с).

    Failures DEBUG: best-effort delete — ack-message в чате тоже не
    катастрофа. Может fail'нуть если юзер сам удалил ack или Telegram
    rate-limit'нул deleteMessage."""
    try:
        await client.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramDeliveryError as exc:
        logger.debug(
            "ack delete failed status=%s msg_id=%s",
            exc.status_code, message_id,
        )
    except Exception:  # noqa: BLE001
        logger.debug("ack delete crashed", exc_info=True)


async def _process_approved_turn(
    *,
    bot_key: str,
    payload: dict,
    onboarding,
    inbound_message_id: str,
    bot_token: str,
) -> None:
    """Run full approved-user processing detached from webhook response.

    2026-04-30 (incident voice-webhook timeout): Telegram webhook delivery
    имеет cutoff ~10 сек. Voice + multi-iter LLM runs регулярно занимают
    16-20 сек, вебхук возвращал 202 после полной обработки → Telegram
    считал доставку failed → ставил retry в очередь → pending update
    застревал → юзер видел «не отвечает».

    Решение: webhook handler возвращает 202 СРАЗУ после persist_inbound,
    основная обработка едет здесь как detached task с собственной DB
    session (request-scoped session уже закрыта к этому моменту).
    """
    SessionLocal = get_session_factory()
    bg_session: Session = SessionLocal()
    try:
        telegram_client = TelegramClient(bot_token)
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

        ack_task: asyncio.Task | None = None
        if (
            message_type in ("text", "voice")
            and not onboarding.is_new_user
        ):
            ack_text = pick_ack()
            ack_task = asyncio.create_task(
                _fire_and_forget_ack(
                    telegram_client, onboarding.chat_id, ack_text,
                ),
                name=f"ack:{onboarding.chat_id}",
            )

        try:
            await handle_telegram_interaction(
                bg_session,
                bot_key=bot_key,
                payload=payload,
                telegram_client=telegram_client,
                onboarding=onboarding,
                inbound_message_id=inbound_message_id,
            )
        except TelegramDeliveryError as exc:
            logger.warning(
                "Telegram delivery failed during webhook handling: %s", exc,
            )
        finally:
            ack_message_id: int | None = None
            if ack_task is not None:
                if not ack_task.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(ack_task), timeout=2.0,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "ack task still pending after main turn — abandoning",
                        )
                if ack_task.done() and not ack_task.cancelled():
                    try:
                        ack_message_id = ack_task.result()
                    except Exception:  # noqa: BLE001
                        ack_message_id = None

            reply_delivered_inline = (
                trace_ctx is not None and any(
                    e.step == "outbox.delivered" for e in trace_ctx.events
                )
            )
            if (
                ack_message_id is not None
                and reply_delivered_inline
                and onboarding.chat_id is not None
            ):
                asyncio.create_task(
                    _delete_ack_after_reply(
                        telegram_client,
                        str(onboarding.chat_id),
                        ack_message_id,
                    ),
                    name=f"ack_del:{onboarding.chat_id}",
                )

            if trace_ctx is not None and not any(
                e.step == "outbox.enqueued" for e in trace_ctx.events
            ):
                trace.emit_block(trace_ctx)
    except Exception:  # noqa: BLE001
        logger.exception("background turn processing crashed")
    finally:
        bg_session.close()


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
    background_tasks: BackgroundTasks,
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

            # 2026-04-29: edit-based wizard. Если callback — эдитим
            # тот же message (wizard navigation prev/next). Если text/
            # voice — send_message нового intro (юзер ещё не в туре).
            #
            # branch резолвим через `pending_bot.match()` который
            # возвращает PendingReply, но нам нужно знать сам branch
            # name для `build_navigation_keyboard`. Если callback с
            # `pb:<branch>` — извлекаем branch напрямую. Иначе —
            # fallback на "intro" (новый юзер).
            from sreda.services.pending_bot import (
                _BRANCHES as _PB_BRANCHES,  # noqa: F401  (легковесный import)
            )
            current_branch = "intro"
            if is_callback and input_text:
                raw = input_text.removeprefix("pb:").strip()
                if raw in _PB_BRANCHES:
                    current_branch = raw
            keyboard = pending_bot.build_navigation_keyboard(current_branch)

            cb_message = (
                callback_query.get("message")
                if isinstance(callback_query, dict) else None
            )
            cb_msg_id = (
                cb_message.get("message_id")
                if isinstance(cb_message, dict) else None
            )

            edited = False
            if is_callback and cb_msg_id is not None:
                try:
                    await pending_client.edit_message_text(
                        chat_id=str(onboarding.chat_id),
                        message_id=int(cb_msg_id),
                        text=reply.text,
                        reply_markup=keyboard,
                    )
                    edited = True
                except TelegramDeliveryError as exc:
                    if exc.status_code == 400 and "not modified" in (str(exc) or "").lower():
                        edited = True
                    else:
                        logger.info(
                            "pending: editMessageText failed status=%s — "
                            "fallback to send_message",
                            exc.status_code,
                        )

            if not edited:
                try:
                    await pending_client.send_message(
                        chat_id=onboarding.chat_id,
                        text=reply.text,
                        reply_markup=keyboard,
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

    # 2026-04-30 (incident voice-webhook timeout): обработка voice + multi-iter
    # LLM регулярно занимает 16-20 сек, что превышает Telegram webhook timeout
    # (~10 сек). Раньше handler возвращал 202 ПОСЛЕ полной обработки → Telegram
    # классифицировал доставку как failed → ставил retry → pending update
    # застревал → юзер видел «не отвечает». Теперь handler возвращает 202
    # СРАЗУ после persist_inbound, основная обработка едет в FastAPI
    # BackgroundTasks (запускается ПОСЛЕ response.send в Starlette/Uvicorn).
    if settings.telegram_bot_token and onboarding.chat_id:
        background_tasks.add_task(
            _process_approved_turn,
            bot_key=bot_key,
            payload=payload,
            onboarding=onboarding,
            inbound_message_id=result.inbound_message_id,
            bot_token=settings.telegram_bot_token,
        )
    return TelegramWebhookAccepted(ok=True, request_id=result.inbound_message_id)
