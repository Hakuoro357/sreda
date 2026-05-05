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


_VOICE_FEATURE_KEY = "voice_transcription"
_VOICE_MAX_BYTES = 2_000_000  # ~2MB, ≈60s OGG/Opus 16kbps mono


def _extract_max_voice_url(payload: dict) -> str | None:
    """Find audio attachment URL in MAX message payload.

    Probe 2026-05-05: MAX voice update has empty body.text and
    ``body.attachments[].type == "audio"`` with payload.url — signed URL
    ready for direct httpx GET (no auth header, signature in query).
    """
    msg = payload.get("message")
    if not isinstance(msg, dict):
        return None
    body = msg.get("body")
    if not isinstance(body, dict):
        return None
    attachments = body.get("attachments")
    if not isinstance(attachments, list):
        return None
    for att in attachments:
        if not isinstance(att, dict) or att.get("type") != "audio":
            continue
        att_payload = att.get("payload")
        if isinstance(att_payload, dict):
            url = att_payload.get("url")
            if isinstance(url, str) and url:
                return url
    return None


async def _maybe_transcribe_max_voice(
    payload: dict,
    *,
    session,
    max_client,
    onboarding,
) -> dict | None:
    """If MAX payload contains audio attachment, transcribe → inject text.

    Mirror ``services.telegram_bot._maybe_transcribe_voice`` с deltas:
    - Detection: ``body.attachments[].type=='audio'`` (не ``message.voice``)
    - Download: один httpx GET signed URL (не двухступенчатый ``getFile``)
    - Duration limit: byte-cap (~2MB ≈ 60s) т.к. MAX не возвращает
      duration в payload
    - Error replies via ``max_client.send_message`` inline (MVP, как TG)

    Returns:
        Updated payload (с injected ``message.body.text``) — продолжаем
        обработку как text turn.
        ``None`` — ошибка отправлена юзеру, processing должен остановиться.
        Same payload (unchanged) если не voice — caller продолжает обычно.
    """
    audio_url = _extract_max_voice_url(payload)
    if audio_url is None:
        return payload  # not voice — passthrough

    chat_id = onboarding.max_chat_id
    tenant_id = onboarding.tenant_id

    async def _send_error(text: str) -> None:
        try:
            await max_client.send_message(
                recipient={"chat_id": chat_id}, text=text,
            )
        except Exception:  # noqa: BLE001
            logger.warning("max voice: error reply failed", exc_info=True)

    # 1. Voice feature module installed
    from sreda.features.app_registry import get_feature_registry
    registry = get_feature_registry()
    if _VOICE_FEATURE_KEY not in registry.modules:
        await _send_error(
            "Голосовые сообщения доступны в подписке. "
            "Открой /subscriptions, чтобы узнать подробнее."
        )
        return None

    # 2. Tenant has active agent с voice access
    from sreda.services.agent_capabilities import has_voice_access
    if not tenant_id or not has_voice_access(session, tenant_id):
        await _send_error(
            "Голосовые сообщения доступны в подписке. "
            "Открой /subscriptions, чтобы узнать подробнее."
        )
        return None

    # 3. Speech recognizer configured
    settings = get_settings()
    from sreda.services.speech.factory import get_speech_recognizer
    recognizer = get_speech_recognizer(settings)
    if recognizer is None:
        await _send_error(
            "Голосовые сообщения сейчас не работают. "
            "Напиши текстом или попробуй позже."
        )
        return None

    # 4 + 5: Download + transcribe (same trace steps as TG для cross-channel
    # ops dashboard parity).
    from sreda.services import trace
    from sreda.services.speech.base import SpeechRecognitionError

    provider = settings.speech_provider or "unknown"

    with trace.step("voice.download", provider="max") as _dl_meta:
        try:
            audio_bytes = await max_client.download_audio(audio_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("max voice download failed: %s", exc)
            await _send_error(
                "Не удалось получить голосовое сообщение. Отправь ещё раз."
            )
            _dl_meta["status"] = "download_failed"
            return None
        _dl_meta["bytes_in"] = len(audio_bytes)

        # Byte-cap (proxy для duration т.к. MAX не возвращает duration).
        if len(audio_bytes) > _VOICE_MAX_BYTES:
            await _send_error(
                "Голосовое слишком длинное. "
                "Отправь покороче (до ~60 секунд)."
            )
            _dl_meta["status"] = "too_long"
            return None

    with trace.step("voice.transcribe", provider=provider) as _trace_meta:
        _trace_meta["bytes_in"] = len(audio_bytes)
        try:
            text = await recognizer.recognize(audio_bytes)
        except SpeechRecognitionError as exc:
            logger.warning("max voice STT failed: %s", exc)
            await _send_error(
                "Не получилось расшифровать голос. "
                "Отправь ещё раз или напиши."
            )
            _trace_meta["status"] = "recognize_failed"
            return None
        _trace_meta["chars_out"] = len(text)
        _trace_meta["status"] = "ok"

    # 6. Record budget usage (1 credit per voice)
    from sreda.services.budget import BudgetService
    BudgetService(session).record_api_usage(
        tenant_id=tenant_id,
        feature_key=_VOICE_FEATURE_KEY,
        provider_key=settings.speech_provider or "unknown",
        task_type="speech_recognition",
        credits_consumed=1,
    )

    # 7. Inject text — downstream dispatch_max_action подхватит как
    # обычный text turn (``_extract_max_message_text`` читает body.text).
    msg = payload.get("message")
    if isinstance(msg, dict):
        body = msg.setdefault("body", {})
        if isinstance(body, dict):
            body["text"] = text
    return payload


async def _process_approved_max_turn(
    *,
    bot_key: str,
    payload: dict,
    onboarding,  # MaxOnboardingResult
    inbound_message_id: str,
) -> None:
    """Run the heavy approved-tenant turn for a MAX update.

    Mirror ``services.telegram_inbound._process_approved_turn_locked``
    с двумя отличиями:
    - Нет ack flow (TG-only fire-and-forget UX). Можно добавить позже когда
      MAX поддержит editMessageText или аналог.
    - Нет inline-send. ``ActionRuntimeService(telegram_client=None)`` →
      runtime пишет outbox row с ``channel_type='max'`` и status='pending';
      ``OutboxDeliveryWorker._send_now_max`` подхватывает и доставляет
      через ``MaxClient.send_message``.

    Per-tenant lock используем shared с TG (``_get_tenant_lock`` из
    telegram_inbound) — если юзер связал каналы и шлёт одновременно,
    обработка сериализуется per tenant_id чтобы conversation context
    оставался coherent.
    """
    # Late imports — избегаем circular: runtime/dispatcher ← onboarding
    # (через type hint).
    from sreda.integrations.max import MaxClient
    from sreda.runtime.dispatcher import dispatch_max_action
    from sreda.runtime.executor import ActionRuntimeService
    from sreda.services.tenant_lock import get_tenant_lock

    SessionLocal = get_session_factory()
    bg_session = SessionLocal()
    settings = get_settings()
    try:
        _set_processing_status(
            bg_session, inbound_message_id, "processing_started",
        )

        # Voice → STT перед dispatch'ем. Если payload содержит audio
        # attachment, transcribe + inject text → dispatch видит обычный
        # text turn. Если voice processing бросил ошибку юзеру (no token,
        # no recognizer, etc.) — возвращает None, мы тут останавливаемся.
        if settings.max_bot_token:
            max_client = MaxClient(token=settings.max_bot_token)
            transcribed = await _maybe_transcribe_max_voice(
                payload,
                session=bg_session,
                max_client=max_client,
                onboarding=onboarding,
            )
            if transcribed is None:
                # Error reply отправлен юзеру inline. Помечаем как
                # ignored — turn закончен, не запускаем conversation graph.
                _set_processing_status(
                    bg_session, inbound_message_id, "ignored",
                )
                return
            payload = transcribed

        action = dispatch_max_action(
            payload=payload,
            bot_key=bot_key,
            onboarding=onboarding,
            inbound_message_id=inbound_message_id,
        )
        if action is None:
            logger.info(
                "max approved: no action resolved tenant=%s inbound=%s "
                "update_type=%s — ignored",
                onboarding.tenant_id, inbound_message_id,
                payload.get("update_type"),
            )
            _set_processing_status(bg_session, inbound_message_id, "ignored")
            return

        tenant_lock = get_tenant_lock(onboarding.tenant_id)
        if tenant_lock.locked():
            logger.info(
                "max tenant turn queued behind in-flight: tenant=%s inbound=%s",
                onboarding.tenant_id, inbound_message_id,
            )
        async with tenant_lock:
            runtime = ActionRuntimeService(bg_session, telegram_client=None)
            queued = runtime.enqueue_action(action)
            await runtime.process_job(queued.job_id)

        _set_processing_status(bg_session, inbound_message_id, "processed")
    except Exception:  # noqa: BLE001
        # Symmetry с TG ``_process_approved_turn_locked``:
        # processing_status НЕ помечается 'failed' on exception — row
        # остаётся 'processing_started' и подхватывается monitor probe
        # ``unprocessed_inbound`` (см. ops dashboard). Объяснение
        # дизайн-решения: 'failed' status сделал бы row невидимым для
        # monitor'а, а нам нужно видеть stuck inbound'ы.
        logger.exception(
            "max approved turn crashed: tenant=%s inbound=%s",
            onboarding.tenant_id, inbound_message_id,
        )
    finally:
        bg_session.close()


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
