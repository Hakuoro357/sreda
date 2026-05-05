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
from sreda.integrations.max import MaxClient
from sreda.integrations.max.client import MaxDeliveryError
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
# Cap shared с MaxClient._MAX_AUDIO_DOWNLOAD_BYTES чтобы UX-сообщение
# «до 30 секунд» не разъехалось с фактическим streaming early-abort'ом
# (codex R3 MINOR — раньше cap дублировался, мог разойтись при правке).
# 1MB ≈ 30s OGG/Opus 16kbps mono — выровнено с Yandex sync STT 30s limit
# (codex R1 MAJOR #4). MaxClient импортирован module-level (codex R4
# MINOR — было late import после function defs, ruff E402).
# NB: byte-cap проверка в _maybe_transcribe_max_voice сама по себе
# unreachable т.к. download_audio aborts streaming раньше — её
# оставляем как defence-in-depth, но реальный «слишком длинное» UX
# идёт через ловлю MaxDeliveryError(status_code=413).
_VOICE_MAX_BYTES = MaxClient._MAX_AUDIO_DOWNLOAD_BYTES


def _extract_max_voice_url(payload: dict) -> str | None:
    """Find audio attachment URL in MAX message payload.

    Probe 2026-05-05: MAX voice update has empty body.text and
    ``body.attachments[].type == "audio"`` with payload.url — signed URL
    ready for direct httpx GET (no auth header, signature in query).

    Codex R1 MAJOR #8: если ``body.text`` non-empty (audio с caption),
    воспринимаем text как намерение юзера; voice processing skip'аем
    чтобы не overwrite'ить caption нашим STT. Юзер может прислать audio
    с подписью где caption — то что хочет сказать боту, а audio просто
    file. Возвращаем None в этом случае.
    """
    msg = payload.get("message")
    if not isinstance(msg, dict):
        return None
    body = msg.get("body")
    if not isinstance(body, dict):
        return None
    # Каптион имеет приоритет — не лезем с STT
    text = body.get("text")
    if isinstance(text, str) and text.strip():
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
    inbound_message_id: str | None = None,
) -> dict | None:
    """If MAX payload contains audio attachment, transcribe → inject text.

    Mirror ``services.telegram_bot._maybe_transcribe_voice`` с deltas:
    - Detection: ``body.attachments[].type=='audio'`` (не ``message.voice``)
    - Download: один streaming httpx GET signed URL (не двухступенчатый
      ``getFile``), early-abort на > _VOICE_MAX_BYTES (1MB ≈ 30s OGG/Opus
      mono — выровнено с Yandex sync STT 30s limit)
    - Duration limit: 1MB byte-cap (codex R3) — proxy для duration т.к.
      MAX не возвращает duration в payload. Превышение → MaxClient
      raises MaxDeliveryError(status_code=413) → user видит «слишком
      длинное», не generic «не удалось получить» (codex R4)
    - Error replies via ``max_client.send_message`` inline (MVP, как TG;
      outbox-based typed error queue — отдельный follow-up)

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
        except MaxDeliveryError as exc:
            # codex R4 MAJOR new: oversize specifically — download_audio
            # streaming-обрывает на > _MAX_AUDIO_DOWNLOAD_BYTES и raises
            # status_code=413. Без этой ветки юзер видел generic
            # «не удалось получить» вместо точного «слишком длинное».
            if exc.status_code == 413:
                logger.info(
                    "max voice oversize: tenant=%s — sending too-long error",
                    tenant_id,
                )
                await _send_error(
                    "Голосовое слишком длинное. "
                    "Отправь покороче (до ~30 секунд)."
                )
                _dl_meta["status"] = "too_long"
                return None
            logger.warning("max voice download failed: %s", exc)
            await _send_error(
                "Не удалось получить голосовое сообщение. Отправь ещё раз."
            )
            _dl_meta["status"] = "download_failed"
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("max voice download crashed: %s", exc)
            await _send_error(
                "Не удалось получить голосовое сообщение. Отправь ещё раз."
            )
            _dl_meta["status"] = "download_crashed"
            return None
        _dl_meta["bytes_in"] = len(audio_bytes)
        # Codex R2 MAJOR #3 partial fix: capture audio magic bytes для
        # diagnostic. Если в проде увидим что MAX шлёт не-OGG/Opus —
        # узнаем какой формат и сможем добавить provider routing
        # (groq поддерживает шире, yandex — только OGG/Opus). Без этого
        # SpeechRecognitionError было бы единственным сигналом.
        if audio_bytes:
            magic = audio_bytes[:4]
            _dl_meta["magic_hex"] = magic.hex()
            # Известные signatures:
            # b"OggS" → OGG/Opus (TG/MAX expected)
            # b"RIFF" → WAV
            # b"ID3\x03" / b"\xFF\xFB" → MP3
            # b"ftyp" (offset 4) → MP4/M4A
            if magic.startswith(b"OggS"):
                _dl_meta["format_guess"] = "ogg"
            elif magic == b"RIFF":
                _dl_meta["format_guess"] = "wav"
            elif magic.startswith(b"ID3") or magic.startswith(b"\xff\xfb"):
                _dl_meta["format_guess"] = "mp3"
            elif len(audio_bytes) > 8 and audio_bytes[4:8] == b"ftyp":
                _dl_meta["format_guess"] = "mp4_m4a"
            else:
                _dl_meta["format_guess"] = "unknown"

        # Byte-cap (proxy для duration т.к. MAX не возвращает duration).
        # Codex R1 MAJOR #4: 30s STT limit (Yandex sync REST) — UX-фраза
        # выровнена с реальным provider'ским ограничением.
        if len(audio_bytes) > _VOICE_MAX_BYTES:
            await _send_error(
                "Голосовое слишком длинное. "
                "Отправь покороче (до ~30 секунд)."
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

    # Codex R5 MINOR fix: ordering — persist transcript ПЕРВЫМ, budget
    # ВТОРЫМ. Reasoning: если sanitize/persist бросит → except + rollback
    # → НЕ хотим charge юзера за un-persisted transcript. Если budget
    # бросит → transcript уже сохранён, juser получит ответ — потеря
    # 1 credit'а accounting'а acceptable (re-import через admin tool).
    # Раньше budget был перед persist — risk «charge без transcript'а»
    # на rollback'е.

    # 6. Persist sanitized transcript на inbound row (codex R1 MAJOR #6).
    # Если процесс крашнется после STT но до завершения turn'а — без
    # этой записи мы потеряли transcript (signed URL уже истечёт через 24h
    # и retry скачать не сможем). С persist'ом — у нас есть text для
    # ручного re-process'инга через admin tool.
    if inbound_message_id is not None:
        try:
            from sreda.db.models.core import InboundMessage
            from sreda.services.privacy_guard import get_default_privacy_guard

            # TextSanitizationResult API (codex R2 fix — было `.matches`,
            # его не существует, AttributeError упал бы в except и persist
            # тихо не работал). Реальные attrs: ``entities`` (list) +
            # ``contains_sensitive_data`` property.
            sanitized_result = get_default_privacy_guard().sanitize_text(text)
            sanitized_text = (
                sanitized_result.sanitized_text if sanitized_result else None
            )
            row = session.get(InboundMessage, inbound_message_id)
            if row is not None:
                row.message_text_sanitized = sanitized_text
                if (
                    sanitized_result is not None
                    and sanitized_result.contains_sensitive_data
                ):
                    row.contains_sensitive_data = True
                session.commit()
        except Exception:  # noqa: BLE001
            # Не убиваем turn если persist не удался — text уже в payload
            # для in-memory dispatch'а.
            logger.warning(
                "max voice: transcript persist failed for inbound=%s",
                inbound_message_id, exc_info=True,
            )
            session.rollback()

    # 7. Inject text into payload — downstream dispatch_max_action видит
    # обычный text turn (``_extract_max_message_text`` читает body.text).
    msg = payload.get("message")
    if isinstance(msg, dict):
        body = msg.setdefault("body", {})
        if isinstance(body, dict):
            body["text"] = text

    # 8. Record budget usage — после persist'а (codex R5 ordering fix).
    # Если сюда дошли — STT и persist прошли; charge корректно.
    from sreda.services.budget import BudgetService
    BudgetService(session).record_api_usage(
        tenant_id=tenant_id,
        feature_key=_VOICE_FEATURE_KEY,
        provider_key=settings.speech_provider or "unknown",
        task_type="speech_recognition",
        credits_consumed=1,
    )

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
    # (через type hint). MaxClient теперь module-level (codex R4 fix).
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

        # Codex R1 MAJOR #5: voice STT идёт ВНУТРИ tenant_lock'а.
        # До рефакторинга STT (1-3s сетевой+CPU) был ДО lock'а — если
        # юзер шлёт быстро voice + text, text-turn мог обогнать voice,
        # порядок conversation history стал бы реверсивен. Теперь оба
        # turn'а serialize'аются per tenant_id.
        tenant_lock = get_tenant_lock(onboarding.tenant_id)
        if tenant_lock.locked():
            logger.info(
                "max tenant turn queued behind in-flight: tenant=%s inbound=%s",
                onboarding.tenant_id, inbound_message_id,
            )
        async with tenant_lock:
            # message_callback (inline-button tap): обработать ДО
            # dispatch'а / voice. Inline-handlers (rem_done/rem_snooze/
            # btn_reply/pb) выполняются здесь — DB updates + answer_callback
            # — и возвращают True если turn полностью обработан. Остальные
            # callback prefixes (billing/profile/eds) идут через dispatcher.
            if payload.get("update_type") == "message_callback":
                if settings.max_bot_token:
                    max_client_cb = MaxClient(token=settings.max_bot_token)
                    handled = await _handle_max_callback(
                        session=bg_session,
                        max_client=max_client_cb,
                        payload=payload,
                        onboarding=onboarding,
                    )
                    if handled:
                        _set_processing_status(
                            bg_session, inbound_message_id, "processed",
                        )
                        return
                else:
                    logger.warning(
                        "max callback received but no max_bot_token; "
                        "skipping ack — tenant=%s",
                        onboarding.tenant_id,
                    )

            # Voice → STT перед dispatch'ем. Если payload содержит audio
            # attachment, transcribe + inject text → dispatch видит обычный
            # text turn. Если voice processing бросил ошибку юзеру (no
            # token, no recognizer, etc.) — возвращает None, мы тут
            # останавливаемся.
            #
            # Skip для message_callback: callback payload не содержит
            # audio_url (только original message с inline-buttons), STT
            # был бы no-op-ом возвращающим payload unchanged. Code-reviewer
            # MEDIUM 2026-05-05: убираем лишнюю работу.
            is_callback = payload.get("update_type") == "message_callback"
            if settings.max_bot_token and not is_callback:
                max_client = MaxClient(token=settings.max_bot_token)
                transcribed = await _maybe_transcribe_max_voice(
                    payload,
                    session=bg_session,
                    max_client=max_client,
                    onboarding=onboarding,
                    inbound_message_id=inbound_message_id,
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
                _set_processing_status(
                    bg_session, inbound_message_id, "ignored",
                )
                return

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


async def _handle_max_callback(
    *,
    session,
    max_client,
    payload: dict,
    onboarding,
) -> bool:
    """Handle MAX inline-button tap.

    Mirror ``services.telegram_bot._handle_callback``:
    - rem_done:/rem_snooze: → FamilyReminder state update + ack toast
    - btn_reply:<token> → resolve token → inject as message text → re-dispatch
    - pb:<branch> → pending-bot tour navigation (TG-only сейчас, для MAX
      пока silent ack)
    - все остальные prefixes → False (caller рутит через dispatcher)

    Returns True если turn полностью обработан (inline path); False если
    caller должен продолжить через dispatch_max_action.

    Best-effort на ack — если answer_callback падает, log warn но не
    breaking. MAX не retry'ит callbacks (по нашему observation), так
    что failed ack = silent UX miss, не loop.
    """
    callback = payload.get("callback")
    if not isinstance(callback, dict):
        return False
    callback_id = callback.get("callback_id")
    data = callback.get("payload")
    if not isinstance(data, str):
        return False

    # rem_done: / rem_snooze: — reminder ack flow
    if data.startswith("rem_done:") or data.startswith("rem_snooze:"):
        await _handle_max_reminder_callback(
            session=session,
            max_client=max_client,
            callback_id=callback_id,
            data=data,
            payload=payload,
            onboarding=onboarding,
        )
        return True

    # Прочие callbacks (billing/profile/eds/pb/btn_reply) пока — generic ack +
    # дальше через dispatcher. pb:/btn_reply: handlers — TG-specific, для
    # MAX отдельный issue (нет 1:1 editMessageText API parity yet).
    if callback_id:
        try:
            await max_client.answer_callback(
                str(callback_id), notification="",
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "max callback ack failed (data=%r) — continuing",
                data, exc_info=True,
            )
    return False


async def _handle_max_reminder_callback(
    *,
    session,
    max_client,
    callback_id,
    data: str,
    payload: dict,
    onboarding,
) -> None:
    """Handle "Готово ✅" / "Отложить ⏰" buttons on a housewife reminder.

    Mirror ``services.telegram_bot._handle_reminder_callback``, но с
    MAX-специфичным UX:

    Per probe 2026-05-05 PM (Boris live test): MAX игнорирует
    ``notification`` toast в DM, юзер не видит feedback. Решение —
    использовать ``message`` field в ``POST /answers`` body, который
    заменяет original message целиком (как TG editMessageText). Юзер
    видит "🔔 X" → "✅ X" с пропавшими кнопками, identical к TG UX.

    Lookup FamilyReminder → acknowledge/snooze → ack с replacement
    message. Cross-tenant defensive check сохранён.
    """
    from sreda.db.models.housewife import FamilyReminder
    from sreda.services.housewife_reminders import (
        SNOOZE_DEFAULT_MINUTES,
        HousewifeReminderService,
    )

    action, _, reminder_id = data.partition(":")

    # Original message text для context в replacement.
    # Probe 2026-05-05 PM #2: на повторный tap (после первого ack)
    # original_text может уже начинаться с "✅"/"⏰" — strip эти маркеры
    # тоже, чтобы не накапливалось "✅ ✅ ✅ ...". Symbol set: bell + чек +
    # будильник + space.
    _CLEANUP_PREFIX = "🔔✅⏰ \t"
    original_text = ""
    msg = payload.get("message")
    if isinstance(msg, dict):
        body = msg.get("body")
        if isinstance(body, dict):
            txt = body.get("text")
            if isinstance(txt, str):
                original_text = txt.lstrip(_CLEANUP_PREFIX).strip()

    async def _ack_with_replacement(new_text: str) -> None:
        """Send /answers с message field — replaces original message body
        (clears buttons + changes text). Fallback на notification если
        replacement отвергнут MAX'ом.

        Probe 2026-05-05 PM #2: ``attachments: []`` обязателен чтобы убрать
        inline-кнопки. Без него MAX оставляет original buttons (юзер
        продолжает тапать впустую).
        """
        if not callback_id:
            return
        try:
            await max_client.answer_callback(
                str(callback_id),
                message={"text": new_text, "attachments": []},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "max reminder cb replacement failed (action=%s): %s — "
                "trying notification fallback",
                action, exc,
            )
            try:
                await max_client.answer_callback(
                    str(callback_id), notification=new_text[:64],
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "max reminder cb notification fallback failed",
                    exc_info=True,
                )

    reminder = (
        session.get(FamilyReminder, reminder_id) if reminder_id else None
    )
    if reminder is None:
        await _ack_with_replacement("Это напоминание уже выполнено.")
        return

    # Cross-tenant defensive check.
    if reminder.tenant_id != onboarding.tenant_id:
        logger.warning(
            "max reminder cb cross-tenant: caller=%s reminder=%s — refused",
            onboarding.tenant_id, reminder.tenant_id,
        )
        await _ack_with_replacement("Это напоминание не ваше.")
        return

    service = HousewifeReminderService(session)
    if action == "rem_done":
        service.acknowledge(reminder)
        replacement = (
            f"✅ {original_text}" if original_text else "✅ Готово"
        )
    else:  # rem_snooze
        service.snooze(reminder, minutes=SNOOZE_DEFAULT_MINUTES)
        suffix = f" (напомню через {SNOOZE_DEFAULT_MINUTES} мин)"
        replacement = (
            f"⏰ {original_text}{suffix}"
            if original_text else f"⏰ Отложено{suffix}"
        )
    session.commit()

    await _ack_with_replacement(replacement)


def _set_processing_status(session, inbound_message_id: str, status: str) -> None:
    """Update inbound_messages.processing_status + commit."""
    from sreda.db.models.core import InboundMessage

    row = session.get(InboundMessage, inbound_message_id)
    if row is not None:
        row.processing_status = status
        session.commit()


def _extract_max_display_name(payload: dict) -> str | None:
    """Best-effort first/last/name из MAX payload (bot_started /
    message_created / message_callback).

    message_callback: юзер в ``payload.callback.user`` — здесь проверяем
    ПЕРВЫМ. ``message.sender`` для callback указывает на бота → не имя
    юзера.
    """
    update_type = payload.get("update_type")

    # message_callback: юзер сидит в callback.user
    if update_type == "message_callback":
        callback = payload.get("callback")
        if isinstance(callback, dict):
            cb_user = callback.get("user")
            if isinstance(cb_user, dict):
                for key in ("name", "first_name"):
                    value = cb_user.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()

    # bot_started: top-level user
    user = payload.get("user")
    if isinstance(user, dict):
        for key in ("name", "first_name"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    # message_created: message.sender — но НЕ для message_callback,
    # короткозамкнули выше.
    if update_type != "message_callback":
        msg = payload.get("message")
        if isinstance(msg, dict):
            sender = msg.get("sender")
            if isinstance(sender, dict):
                for key in ("name", "first_name"):
                    value = sender.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    return None
