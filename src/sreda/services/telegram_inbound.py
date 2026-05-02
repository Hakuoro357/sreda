"""Channel-agnostic Telegram inbound handler.

Single durable-ingest entrypoint shared by both transport modes:

* The webhook route (``/webhooks/telegram/{bot_key}``) calls
  ``await handle_telegram_update(payload, bot_key=...)`` synchronously
  before returning 202. If we returned 202 first and persisted later,
  Telegram would consider the update delivered and never retry — we'd
  lose updates on any crash between ack and persist. Webhook timeout
  budget for the durable-ingest path is < 1 second (DB upsert + INSERT
  + ``asyncio.create_task``); LLM/voice/outbox work is offloaded to the
  detached background task.

* The long-poller (``sreda.workers.telegram_long_poll``) calls the same
  function for each update returned by ``getUpdates`` and only advances
  its in-DB offset after this function has returned (durable ingest →
  offset advance). If the poller crashes between the two commits, the
  next ``getUpdates`` re-delivers the same update; idempotency by
  ``external_update_id`` (= ``persist_telegram_inbound_event.is_duplicate``)
  swallows the dupe and processing continues from there.

Lifecycle of ``inbound_messages.processing_status`` is owned here:

  ingested → processing_started → processed   (approved-user happy path)
                ↘ exception                    (left unchanged → monitor catches)
  ignored                                      (pending tenant / unsupported / service command)

Duplicate update_id is a true no-op: we do not create or update a row;
the existing row keeps whatever status it already had.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.db.models.core import InboundMessage, Tenant
from sreda.db.session import get_session_factory
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.services import trace
from sreda.services.ack_messages import pick_ack
from sreda.services.inbound_messages import persist_telegram_inbound_event
from sreda.services.onboarding import ensure_telegram_user_bundle
from sreda.services.telegram_bot import handle_telegram_interaction

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

logger = logging.getLogger(__name__)


# 2026-04-30: per-tenant async lock — сериализует обработку нескольких
# inbound'ов от одного tenant'а. Когда юзер шлёт 4 voice подряд (incident
# user_tg_755682022: 4 update'а в 150мс), без лока 4 bg-task'а параллельно
# конкурируют за SQLite/PG write → 30-90с залипы на add_checklist_items
# / create_checklist. Lock обеспечивает последовательную обработку: 1-й
# turn run, 2-4 ждут в очереди. Не влияет на разных юзеров.
_TENANT_LOCKS: dict[str, asyncio.Lock] = {}


def _get_tenant_lock(tenant_id: str) -> asyncio.Lock:
    lock = _TENANT_LOCKS.get(tenant_id)
    if lock is None:
        lock = asyncio.Lock()
        _TENANT_LOCKS[tenant_id] = lock
    return lock


async def _fire_and_forget_ack(
    client: TelegramClient, chat_id: str, text: str,
) -> int | None:
    """Send a one-word ack reply concurrently with main turn processing.

    Returns Telegram message_id of the ack (None on failure). Used to
    later call ``delete_message`` after delivering the real reply —
    clean-chat UX (one message per turn instead of two).
    """
    with trace.step("ack.sent", phrase=text) as meta:
        try:
            response = await client.send_message(chat_id=chat_id, text=text)
            meta["status"] = "ok"
            result = response.get("result") or {}
            mid = result.get("message_id")
            # Stage 9.1 (см. tomorrow-plan): TG-side identifiers для
            # диагностики «ack приходит после реплая». Если ack.message_id
            # < final.message_id — Telegram client-side sync, сетью не
            # лечится, нужен placeholder + editMessageText (9.2).
            # Если ack.message_id > final.message_id — реальный transport
            # HOL, нужен WireGuard / Go-egress (9.3).
            if isinstance(mid, int):
                meta["tg_message_id"] = mid
            tg_date = result.get("date")
            if isinstance(tg_date, int):
                meta["tg_date"] = tg_date
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
    """Best-effort delete of the ack message after the real reply lands."""
    try:
        await client.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramDeliveryError as exc:
        logger.debug(
            "ack delete failed status=%s msg_id=%s",
            exc.status_code, message_id,
        )
    except Exception:  # noqa: BLE001
        logger.debug("ack delete crashed", exc_info=True)


def _set_processing_status(
    session: Session, inbound_message_id: str, new_status: str,
) -> None:
    """Update processing_status on inbound_messages row + commit.

    Quiet on missing row (caller passed a stale id) — log and continue;
    the lifecycle is observability sugar, not correctness-critical.
    """
    inbound = session.get(InboundMessage, inbound_message_id)
    if inbound is None:
        logger.warning(
            "processing_status update skipped: inbound %s not found",
            inbound_message_id,
        )
        return
    inbound.processing_status = new_status
    session.commit()


async def _process_approved_turn(
    *,
    bot_key: str,
    payload: dict,
    onboarding,
    inbound_message_id: str,
    bot_token: str,
) -> None:
    """Run full approved-user processing detached from caller.

    2026-04-30 (incident voice-webhook timeout): voice + multi-iter LLM
    runs regularly take 16-20 seconds, exceeding Telegram's webhook
    timeout (~10s). Webhook handler returns 202 ASAP after persist; this
    function runs as a detached task with its own DB session.

    Per-tenant lock prevents 4-voice-burst write contention: multiple
    concurrent turns from the same user serialize.
    """
    tenant_lock = _get_tenant_lock(onboarding.tenant_id)
    if tenant_lock.locked():
        logger.info(
            "tenant turn queued behind in-flight: tenant=%s inbound=%s",
            onboarding.tenant_id, inbound_message_id,
        )
    async with tenant_lock:
        await _process_approved_turn_locked(
            bot_key=bot_key,
            payload=payload,
            onboarding=onboarding,
            inbound_message_id=inbound_message_id,
            bot_token=bot_token,
        )


async def _process_approved_turn_locked(
    *,
    bot_key: str,
    payload: dict,
    onboarding,
    inbound_message_id: str,
    bot_token: str,
) -> None:
    """Inner — runs under the per-tenant lock.

    Updates ``processing_status`` lifecycle:
      - ``processing_started`` at first step (so monitor distinguishes
        «turn was attempted» from «turn never started»)
      - ``processed`` after the orchestrated turn completes successfully
      - on exception: status is left as-is so the
        ``unprocessed_inbound`` monitor probe can pick it up
    """
    SessionLocal = get_session_factory()
    bg_session: Session = SessionLocal()
    try:
        _set_processing_status(
            bg_session, inbound_message_id, "processing_started",
        )

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
                "Telegram delivery failed during turn processing: %s", exc,
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

        # Reached only on the success path (no re-raise above). Mark
        # processed so the unprocessed_inbound monitor stays quiet.
        _set_processing_status(bg_session, inbound_message_id, "processed")
    except Exception:  # noqa: BLE001
        logger.exception("background turn processing crashed")
    finally:
        bg_session.close()


async def _handle_pending_tenant(
    session: Session,
    *,
    payload: dict,
    onboarding,
) -> None:
    """Pending-tenant scripted-bot reply.

    The tenant has not been approved in /admin/users yet. Instead of
    silent-dropping, we run the scripted demo-bot (``pending_bot``) and
    answer with text + inline keyboard. After this returns the inbound
    is considered done (status = ``ignored``) — no LLM, no outbox, no
    background work.
    """
    settings = get_settings()
    if not (settings.telegram_bot_token and onboarding.chat_id):
        logger.info(
            "pending tenant %s — no bot token/chat_id, drop (update_id=%s)",
            onboarding.tenant_id, payload.get("update_id"),
        )
        return

    from sreda.services import pending_bot
    from sreda.services.pending_bot import (
        _BRANCHES as _PB_BRANCHES,  # noqa: F401  (lightweight import)
    )

    message = payload.get("message") if isinstance(payload, dict) else None
    callback_query = (
        payload.get("callback_query")
        if isinstance(payload, dict) else None
    )
    input_text: str | None = None
    is_callback = False
    if isinstance(callback_query, dict):
        cb_data = str(callback_query.get("data") or "")
        if pending_bot.is_pending_callback(cb_data):
            input_text = cb_data
            is_callback = True
        else:
            input_text = None
    elif isinstance(message, dict) and message.get("text"):
        input_text = str(message.get("text"))

    reply = pending_bot.match(input_text, is_callback=is_callback)
    pending_client = TelegramClient(settings.telegram_bot_token)

    cb_id = (
        str(callback_query.get("id") or "")
        if isinstance(callback_query, dict) else ""
    )
    if cb_id:
        try:
            await pending_client.answer_callback_query(cb_id, text="")
        except TelegramDeliveryError:
            pass

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


async def handle_telegram_update(
    payload: dict,
    *,
    bot_key: str = "sreda",
    background_tasks: "BackgroundTasks | None" = None,
) -> str:
    """Durable ingest of one Telegram update.

    Returns ``inbound_message_id`` once ``persist_telegram_inbound_event``
    has committed (the same id is also returned for the duplicate
    short-circuit path so the webhook can still respond with a stable
    request id).

    Idempotent on duplicate ``update_id``: a second call for the same
    update is a true no-op (no row created, no status changed).

    For approved tenants, schedules ``_process_approved_turn`` to run
    detached: when ``background_tasks`` is provided (webhook context)
    we use FastAPI's ``BackgroundTasks`` so the turn runs in the
    request lifecycle and TestClient await semantics work as expected;
    otherwise (long-poller context) we use ``asyncio.create_task`` and
    rely on the long-running event loop. Either way the durable-ingest
    path itself stays inside the webhook ~1s timeout budget.

    For pending tenants, runs the scripted ``pending_bot`` reply
    inline, then marks the inbound as ``ignored``.
    """
    settings = get_settings()
    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        onboarding = ensure_telegram_user_bundle(session, payload)
        result = persist_telegram_inbound_event(
            session, bot_key=bot_key, payload=payload,
        )
        # persist_telegram_inbound_event commits internally — ingest is now
        # durable in the DB and the long-poller may safely advance offset.

        if result.is_duplicate:
            logger.info(
                "telegram inbound: duplicate update_id %s for bot %s — no-op",
                payload.get("update_id"), bot_key,
            )
            return result.inbound_message_id

        tenant = session.get(Tenant, onboarding.tenant_id)
        is_approved = tenant is not None and tenant.approved_at is not None

        if not is_approved:
            await _handle_pending_tenant(
                session, payload=payload, onboarding=onboarding,
            )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        if not (settings.telegram_bot_token and onboarding.chat_id):
            # No bot token / chat_id → cannot run an approved turn.
            # Mark as ignored so the unprocessed_inbound monitor stays
            # quiet; this is a deliberate skip, not a crash.
            logger.info(
                "approved tenant %s — no bot token/chat_id, drop (update_id=%s)",
                onboarding.tenant_id, payload.get("update_id"),
            )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        inbound_message_id = result.inbound_message_id

    # Detach the heavy turn from the caller. `_process_approved_turn`
    # opens its own DB session (the session above has been closed by
    # the `with` block). It is observed only via the inbound row's
    # processing_status: `processing_started` once it runs, `processed`
    # on success, or unchanged (=> caught by monitor) if it crashes.
    if background_tasks is not None:
        background_tasks.add_task(
            _process_approved_turn,
            bot_key=bot_key,
            payload=payload,
            onboarding=onboarding,
            inbound_message_id=inbound_message_id,
            bot_token=settings.telegram_bot_token,
        )
    else:
        asyncio.create_task(
            _process_approved_turn(
                bot_key=bot_key,
                payload=payload,
                onboarding=onboarding,
                inbound_message_id=inbound_message_id,
                bot_token=settings.telegram_bot_token,
            ),
            name=f"approved_turn:{onboarding.tenant_id}:{inbound_message_id}",
        )
    return inbound_message_id
