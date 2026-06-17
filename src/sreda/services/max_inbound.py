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
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sreda.config.settings import get_settings
from sreda.db.models.core import Tenant
from sreda.db.session import get_session_factory
from sreda.integrations.max import MaxClient
from sreda.integrations.max.client import MaxDeliveryError
from sreda.services.inbound_messages import persist_max_inbound_event
from sreda.services.onboarding import ensure_max_user_bundle


# #133: локальный шов для функционального харнеса — патчится
# именно этот символ модуля (подмена asyncio.create_task была бы
# глобальной для всего процесса).
_create_task = asyncio.create_task


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

    # Channel-link interception — handle BEFORE ensure_max_user_bundle
    # чтобы не создать orphan tenant_max_<id> для one-shot link command.
    # MAX delivers `?start=lnk_X` deep-link param как bot_started event с
    # `payload="lnk_<token>"` field. Probe 2026-05-06 confirmed structure.
    # User не существует в DB → consume_link's collision check вернёт
    # None → «no collision» branch → attach to source tenant cleanly.
    if update_type == "bot_started":
        bot_payload = payload.get("payload")
        if isinstance(bot_payload, str) and bot_payload.startswith("lnk_"):
            raw_token = bot_payload[len("lnk_"):].strip()
            await _handle_max_link_start_cmd(
                raw_token=raw_token,
                chat_id=str(chat_id) if chat_id is not None else None,
            )
            return ""

    if update_type == "message_callback":
        callback = payload.get("callback") or {}
        cb_data = callback.get("payload") or ""
        if isinstance(cb_data, str) and cb_data.startswith("confirm_link:"):
            raw_token = cb_data.removeprefix("confirm_link:").strip()
            callback_id = callback.get("callback_id")
            await _handle_max_link_confirm_cb(
                raw_token=raw_token,
                sender_user_id=str(sender_user_id),
                chat_id=str(chat_id) if chat_id is not None else None,
                callback_id=callback_id,
            )
            return ""
        if isinstance(cb_data, str) and cb_data.startswith("cancel_link:"):
            callback_id = callback.get("callback_id")
            await _handle_max_link_cancel_cb(
                raw_token=cb_data.removeprefix("cancel_link:").strip(),
                callback_id=callback_id,
            )
            return ""

    with SessionLocal() as session:
        # Sender display name (best-effort).
        display_name = _extract_max_display_name(payload)
        # Phase 2C: catch SignupBlocked from abuse guard.
        from sreda.services.signup_abuse import SignupBlocked
        from sreda.services.upgrade_copy import UPGRADE_COPY
        try:
            onboarding = ensure_max_user_bundle(
                session,
                max_account_id=sender_user_id,
                max_chat_id=chat_id,
                display_name=display_name,
            )
        except SignupBlocked as exc:
            logger.info(
                "max inbound: signup blocked reason=%s — drop update",
                exc.reason,
            )
            if chat_id and settings.max_bot_token:
                try:
                    client = MaxClient(token=settings.max_bot_token)
                    await client.send_message(
                        recipient={"chat_id": chat_id},
                        text=UPGRADE_COPY.get(exc.reason, UPGRADE_COPY["signups_closed"]),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("max signup-blocked notify failed", exc_info=True)
            return ""

        # Phase 2 fix 2026-05-08 (Codex MAJOR hardening): welcome
        # отправляется на основе `is_welcome_sent` флага в БД, не
        # `is_new_user`. Если HTTP-send падает, флаг остаётся False —
        # следующий inbound заретраит. Mark-sent ТОЛЬКО на успешный
        # send, через `mark_welcome_sent` (commit per call).
        welcome_just_sent = False
        if (
            onboarding.tenant_id
            and onboarding.user_id
            and onboarding.max_chat_id
            and settings.max_bot_token
        ):
            from sreda.services.onboarding import (
                build_post_approve_keyboard_max,
                build_post_approve_message,
                is_welcome_sent,
                mark_welcome_sent,
            )
            if not is_welcome_sent(session, onboarding.tenant_id, onboarding.user_id):
                try:
                    client = MaxClient(token=settings.max_bot_token)
                    await client.send_message(
                        recipient={"chat_id": onboarding.max_chat_id},
                        text=build_post_approve_message(),
                        attachments=build_post_approve_keyboard_max(),
                    )
                    mark_welcome_sent(
                        session, onboarding.tenant_id, onboarding.user_id,
                    )
                    welcome_just_sent = True
                    logger.info(
                        "max inbound: post-approve welcome sent tenant=%s",
                        onboarding.tenant_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "max post-approve welcome failed for tenant=%s "
                        "(retry on next inbound)",
                        onboarding.tenant_id, exc_info=True,
                    )

        result = persist_max_inbound_event(
            session, bot_key=bot_key, payload=payload,
        )

        # 2026-05-09 fix (Boris feedback): если welcome ТОЛЬКО ЧТО отправлен
        # этой inbound-message — НЕ передаём её дальше в chat handler.
        # Иначе юзер получает double-reply на /start: welcome (с кнопкой)
        # + LLM chat reply. Welcome consumes the inbound; следующее
        # сообщение юзера пойдёт нормально в chat. Применяется ко ВСЕМ
        # inbound types — text/voice/callback/bot_started.
        if welcome_just_sent:
            logger.info(
                "max inbound: welcome consumed inbound — skip chat dispatch "
                "tenant=%s inbound_id=%s",
                onboarding.tenant_id, result.inbound_message_id,
            )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        if result.is_duplicate:
            logger.info(
                "max inbound: duplicate update %s for bot %s — no-op",
                result.inbound_message_id, bot_key,
            )
            return result.inbound_message_id

        # Phase 2 (Codex CRITICAL fix 2026-05-07): EntitlementGate enforced
        # at handler entry. Suspended tenants получают UPGRADE_COPY и
        # помечаются ignored. См. подробный коммент в telegram_inbound.py.
        from sreda.services.entitlement_gate import EntitlementGate
        _gate = EntitlementGate(session).check(onboarding.tenant_id)
        if not _gate.allowed:
            logger.info(
                "max inbound: entitlement gate blocked tenant=%s "
                "reason=%s — drop turn, mark ignored",
                onboarding.tenant_id, _gate.reason,
            )
            if onboarding.max_chat_id and settings.max_bot_token:
                try:
                    client = MaxClient(token=settings.max_bot_token)
                    await client.send_message(
                        recipient={"chat_id": onboarding.max_chat_id},
                        text=UPGRADE_COPY.get(
                            _gate.reason,
                            UPGRADE_COPY["no_active_subscription"],
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "max entitlement-blocked notify failed", exc_info=True,
                    )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        tenant = session.get(Tenant, onboarding.tenant_id)
        is_approved = tenant is not None and tenant.approved_at is not None

        # Broadcast pattern (2026-05-09): pb:* callbacks от approved
        # юзеров тоже должны запускать pending_bot wizard. Это позволяет
        # post-approve welcome message с кнопкой «Покажи примеры»
        # → callback `pb:voice` → wizard editMessage flow. Без этой ветки
        # callback'и от approved юзеров silent-drop'ились.
        is_pb_callback = (
            update_type == "message_callback"
            and isinstance(
                ((payload.get("callback") or {}).get("payload") or ""),
                str,
            )
            and ((payload.get("callback") or {}).get("payload") or "")
                .startswith("pb:")
        )

        if not is_approved or is_pb_callback:
            # Pending tenant ИЛИ approved юзер с pb:* callback'ом —
            # send welcome через pending_bot (intro branch при
            # bot_started; tour branch на pb:* callbacks).
            # Иначе silent (избегаем spam'а при regular messages).
            #
            # is_post_approve_tour=True ТОЛЬКО для approved+pb_callback:
            # pb:done покажет вопрос имени и запишет waiting-flag.
            # Pending юзеры получают pending-closing без вопроса имени.
            await _handle_max_pending_tenant(
                session=session,
                payload=payload,
                update_type=update_type,
                onboarding=onboarding,
                settings=settings,
                is_post_approve_tour=is_approved and is_pb_callback,
            )
            _set_processing_status(
                session, result.inbound_message_id, "ignored",
            )
            return result.inbound_message_id

        # 2026-05-11 (Boris explicit Pat 2): MAX channel btn_reply handler.
        # Mirror telegram_bot._handle_btn_reply_callback: resolve token →
        # ack callback → mutate payload to look like text-message →
        # downstream `dispatch_max_action` видит обычный text turn и
        # инжектит label в action.params как user text. Single-use:
        # `ReplyButtonService.resolve_token` помечает `used_at`, повторный
        # клик возвращает None → toast «выбор устарел». Только для
        # approved tenants — кнопки только им и шлются.
        _cb_data_btn = (
            (payload.get("callback") or {}).get("payload") or ""
            if update_type == "message_callback"
            else ""
        )
        if isinstance(_cb_data_btn, str) and _cb_data_btn.startswith("btn_reply:"):
            from sreda.services.reply_buttons import ReplyButtonService
            cb_token = _cb_data_btn.removeprefix("btn_reply:").strip()
            callback_id = (payload.get("callback") or {}).get("callback_id")
            label: str | None = None
            if onboarding.tenant_id and onboarding.user_id and cb_token:
                try:
                    label = ReplyButtonService(session).resolve_token(
                        tenant_id=onboarding.tenant_id,
                        user_id=onboarding.user_id,
                        token=cb_token,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "max btn_reply token resolution failed for "
                        "tenant=%s token=%s",
                        onboarding.tenant_id, cb_token, exc_info=True,
                    )
                    label = None

            if label is None:
                # Expired / already used / wrong owner — toast + drop.
                if callback_id and settings.max_bot_token:
                    try:
                        _client = MaxClient(token=settings.max_bot_token)
                        await _client.answer_callback(
                            str(callback_id),
                            notification="Выбор устарел. Напиши что нужно.",
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "max btn_reply expired-ack failed", exc_info=True,
                        )
                _set_processing_status(
                    session, result.inbound_message_id, "ignored",
                )
                return result.inbound_message_id

            # Label resolved — ack callback с toast и мутируем payload в
            # text-message shape. Downstream `dispatch_max_action` увидит
            # обычный message_created и injectнет label в action.params.text.
            if callback_id and settings.max_bot_token:
                try:
                    _client = MaxClient(token=settings.max_bot_token)
                    await _client.answer_callback(
                        str(callback_id), notification=label,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "max btn_reply ack failed (label=%r)",
                        label, exc_info=True,
                    )
            # Mutate to text-message shape.
            payload["update_type"] = "message_created"
            _msg = payload.get("message")
            if isinstance(_msg, dict):
                _body = _msg.setdefault("body", {})
                if isinstance(_body, dict):
                    _body["text"] = label
            payload.pop("callback", None)
            update_type = "message_created"  # update local var for downstream
            logger.info(
                "max btn_reply resolved tenant=%s user=%s label=%r — "
                "dispatching as text-message",
                onboarding.tenant_id, onboarding.user_id, label,
            )

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

        # R-19 (2026-05-13): drop unknown-prefix callbacks SYNCHRONOUSLY.
        # Production incident 2026-05-12 18:58 UTC — 2 callback events
        # с opaque MAX-generated payload (echo от bot's own outbound
        # message c inline-кнопками) застряли с processing_status='ingested'
        # навечно → monitor alert «unprocessed_inbound».
        #
        # Корень: `_handle_max_callback` (line 1107) routит только known
        # prefixes (btn_reply:/pb:/rem_done:/rem_snooze:). Unknown prefix
        # → returns False → caller спавнит background task → если task
        # не start'ит (FastAPI BackgroundTasks race / asyncio loop close)
        # — status НИКОГДА не updates → stuck в 'ingested' → false
        # positive monitor alert.
        #
        # Defensive fix: detect unknown callback prefix СИНХРОННО и mark
        # 'ignored' до spawning task. Никакого user-facing ответа
        # (избегаем spam от echo events).
        if _is_unknown_max_callback_prefix(payload):
            cb_payload_str = _max_callback_payload(payload) or ""
            logger.info(
                "max callback unknown prefix tenant=%s inbound=%s "
                "payload_first=%r — sync drop (R-19 defensive)",
                onboarding.tenant_id, result.inbound_message_id,
                cb_payload_str[:40],
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
        _create_task(
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

    # --- Phase 2C: free-tier quota gate (mirror TG) ---
    from sreda.services.entitlement_gate import EntitlementGate
    from sreda.services.upgrade_copy import UPGRADE_COPY
    from sreda.services.usage_ledger import (
        SREDA_FREE_LLM_DAILY, SREDA_FREE_LLM_MONTHLY,
        SREDA_FREE_VOICE_SECONDS_DAILY, SREDA_FREE_VOICE_SECONDS_MONTHLY,
        UsageLedgerService, msk_period_keys,
    )

    _gate = EntitlementGate(session).check(tenant_id)
    _is_free = (_gate.plan_key == "sreda_free" and not _gate.is_grandfathered)
    _ledger: UsageLedgerService | None = None
    _llm_periods: list[tuple[str, str, int]] | None = None
    _voice_periods: list[tuple[str, str, int]] | None = None
    _duration_seconds: float | None = None

    if _is_free:
        _daily_key, _monthly_key = msk_period_keys()
        _ledger = UsageLedgerService(session.get_bind())
        _llm_periods = [
            ("daily", _daily_key, SREDA_FREE_LLM_DAILY),
            ("monthly", _monthly_key, SREDA_FREE_LLM_MONTHLY),
        ]
        if not _ledger.try_consume(tenant_id, "llm_turns", 1, _llm_periods):
            await _send_error(UPGRADE_COPY["llm_daily_or_monthly"])
            return None
        _voice_periods = [
            ("daily", _daily_key, SREDA_FREE_VOICE_SECONDS_DAILY),
            ("monthly", _monthly_key, SREDA_FREE_VOICE_SECONDS_MONTHLY),
        ]

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
                # Phase 2C M1 fix: refund LLM (reserved before download)
                if _is_free and _ledger and _llm_periods:
                    _ledger.refund(tenant_id, "llm_turns", 1, _llm_periods)
                return None
            logger.warning("max voice download failed: %s", exc)
            await _send_error(
                "Не удалось получить голосовое сообщение. Отправь ещё раз."
            )
            _dl_meta["status"] = "download_failed"
            if _is_free and _ledger and _llm_periods:
                _ledger.refund(tenant_id, "llm_turns", 1, _llm_periods)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("max voice download crashed: %s", exc)
            await _send_error(
                "Не удалось получить голосовое сообщение. Отправь ещё раз."
            )
            _dl_meta["status"] = "download_crashed"
            if _is_free and _ledger and _llm_periods:
                _ledger.refund(tenant_id, "llm_turns", 1, _llm_periods)
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
            # Phase 2C: refund LLM if reserved (we charged at gate)
            if _is_free and _ledger and _llm_periods:
                _ledger.refund(tenant_id, "llm_turns", 1, _llm_periods)
            return None

    # --- Phase 2C: voice quota reserve via ffprobe duration ---
    if _is_free and _ledger and _llm_periods and _voice_periods:
        from sreda.services.audio_probe import FfprobeError, ffprobe_duration
        try:
            _duration_seconds = ffprobe_duration(audio_bytes)
        except FfprobeError as exc:
            logger.warning("max voice ffprobe failed: %s — refunding LLM", exc)
            _ledger.refund(tenant_id, "llm_turns", 1, _llm_periods)
            await _send_error(
                "Не получилось обработать голосовое — напиши текстом, пожалуйста."
            )
            return None
        if not _ledger.try_consume(
            tenant_id, "voice_stt_seconds", _duration_seconds, _voice_periods,
        ):
            _ledger.refund(tenant_id, "llm_turns", 1, _llm_periods)
            await _send_error(UPGRADE_COPY["voice_daily_or_monthly"])
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
            # Phase 2C: refund both quotas если reserved
            if _is_free and _ledger:
                if _llm_periods:
                    _ledger.refund(tenant_id, "llm_turns", 1, _llm_periods)
                if _voice_periods and _duration_seconds:
                    _ledger.refund(
                        tenant_id, "voice_stt_seconds",
                        _duration_seconds, _voice_periods,
                    )
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
        # Phase 2 (Codex MAJOR-2 fix 2026-05-07): mark payload-level flag
        # чтобы dispatch_max_action пробросил его в action.params и
        # runtime/handlers.py не списал второй llm_turns. См. подробный
        # коммент в telegram_bot.py._maybe_transcribe_voice.
        if _is_free:
            msg["_llm_pre_reserved"] = True

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
    from sreda.services import trace
    from sreda.services.tenant_lock import get_tenant_lock

    SessionLocal = get_session_factory()
    bg_session = SessionLocal()
    settings = get_settings()
    # Cut-off time для outbox correlation (используется в ack delete polling).
    turn_started_at = datetime.now(timezone.utc)
    try:
        _set_processing_status(
            bg_session, inbound_message_id, "processing_started",
        )

        # Phase 1A trace instrumentation 2026-05-08 (parity с TG).
        # Раньше MAX inbound НЕ открывал trace context — все
        # `trace.step()` вызовы внутри handlers/graph были no-op.
        # Теперь MAX channel тоже виден в /var/log/sreda/trace.log
        # с per-stage breakdown.
        trace.start_trace(
            user_id=onboarding.user_id,
            tenant_id=onboarding.tenant_id,
            channel="max",
        )
        msg = payload.get("message") if isinstance(payload, dict) else None
        update_type = payload.get("update_type") if isinstance(payload, dict) else "?"
        msg_kind = "callback" if update_type == "message_callback" else (
            "voice" if (
                isinstance(msg, dict)
                and isinstance(msg.get("body"), dict)
                and any(
                    a.get("type") == "audio"
                    for a in (msg["body"].get("attachments") or [])
                )
            ) else "text"
        )
        trace.record("webhook.received", type=msg_kind, channel="max")

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

                from sreda.runtime.dispatcher import _extract_max_message_text
                from sreda.services.housewife_persona import (
                    is_persona_settings_request,
                )

                message_text = _extract_max_message_text(payload)
                if (
                    message_text
                    and onboarding.tenant_id
                    and onboarding.user_id
                ):
                    from sreda.services.housewife_onboarding import (
                        enqueue_pb_tour_name_confirmation,
                        enqueue_pb_tour_name_retry,
                        extract_pb_tour_display_name_with_llm,
                        is_pb_tour_waiting_for_name,
                        save_pb_tour_display_name,
                    )

                    try:
                        if is_pb_tour_waiting_for_name(
                            bg_session,
                            tenant_id=onboarding.tenant_id,
                            user_id=onboarding.user_id,
                        ):
                            extracted_name = await asyncio.to_thread(
                                extract_pb_tour_display_name_with_llm,
                                message_text,
                            )
                            if not extracted_name:
                                enqueue_pb_tour_name_retry(
                                    bg_session,
                                    tenant_id=onboarding.tenant_id,
                                    workspace_id=onboarding.workspace_id,
                                    user_id=onboarding.user_id,
                                    channel_type="max",
                                    chat_id=str(onboarding.max_chat_id),
                                )
                                bg_session.commit()
                                _set_processing_status(
                                    bg_session, inbound_message_id, "processed",
                                )
                                return
                            display_name = save_pb_tour_display_name(
                                bg_session,
                                tenant_id=onboarding.tenant_id,
                                user_id=onboarding.user_id,
                                raw_name=extracted_name,
                            )
                            enqueue_pb_tour_name_confirmation(
                                bg_session,
                                tenant_id=onboarding.tenant_id,
                                workspace_id=onboarding.workspace_id,
                                user_id=onboarding.user_id,
                                channel_type="max",
                                chat_id=str(onboarding.max_chat_id),
                                display_name=display_name,
                            )
                            bg_session.commit()
                            _set_processing_status(
                                bg_session, inbound_message_id, "processed",
                            )
                            return
                    except ValueError as exc:
                        bg_session.rollback()
                        logger.info("max post-tour name capture skipped: %s", exc)
                    except Exception:  # noqa: BLE001
                        bg_session.rollback()
                        logger.exception("max post-tour name capture failed")
                        _set_processing_status(
                            bg_session, inbound_message_id, "processed",
                        )
                        return
                if message_text and is_persona_settings_request(message_text):
                    await _handle_max_persona_settings_request(
                        max_client=max_client,
                        onboarding=onboarding,
                    )
                    _set_processing_status(
                        bg_session, inbound_message_id, "processed",
                    )
                    return

                # #66 ГЕЙТ MAX: тенант из react_loop_enabled_tenants + текст →
                # новый ReAct+interrupt-цикл. Ответ шлём напрямую MaxClient
                # (welcome тоже inline-шлёт), ack не создаём, dispatch пропускаем.
                # Остальные тенанты/callback/voice-ошибки/новые юзеры — прежним
                # путём (нулевой регресс). Флаг по умолчанию пуст → no-op.
                if (
                    message_text
                    and not onboarding.is_new_user
                    and onboarding.tenant_id in settings.react_loop_enabled_tenants
                ):
                    from sreda.runtime import react_loop
                    from sreda.services.llm import get_chat_llm

                    _llm = get_chat_llm(
                        provider=settings.planner_provider, settings=settings,
                    )
                    _reply = await react_loop.handle_turn(
                        session=bg_session,
                        tenant_id=onboarding.tenant_id,
                        user_id=onboarding.user_id,
                        thread_id=f"react:{onboarding.tenant_id}:{onboarding.max_chat_id}",
                        llm=_llm,
                        user_text=message_text,
                    )
                    trace.record(
                        "react_loop.replied", chars=len(_reply or ""), channel="max",
                    )
                    await max_client.send_message(
                        recipient={"chat_id": onboarding.max_chat_id}, text=_reply,
                    )
                    _set_processing_status(
                        bg_session, inbound_message_id, "processed",
                    )
                    return

            # Ack message — UX parity с TG: показываем «⏳ Работаю…» как
            # только начали обработку, чтобы юзер не молча ждал 5-15s
            # пока LLM думает + outbox doставляет. Boris directive
            # 2026-05-05: «ack сообщение делаем».
            #
            # Conditions (mirror TG):
            # - не для callback events (там answer_callback уже UX-feedback)
            # - не для new users (у них welcome-flow вместо ack)
            # - только если у нас есть токен для send'а
            #
            # Fire-and-forget — не блокируем main turn. После turn
            # удаляем ack message чтобы chat остался clean (одно
            # bot-message per turn).
            ack_task: asyncio.Task | None = None
            ack_progress_controller = None
            if (
                settings.max_bot_token
                and not is_callback
                and not onboarding.is_new_user
            ):
                from sreda.services.ack_messages import pick_ack
                from sreda.services.ack_progress import MaxAckProgressController
                ack_text = pick_ack()
                max_ack_client = MaxClient(token=settings.max_bot_token)
                ack_task = _create_task(
                    _send_max_ack(
                        token=settings.max_bot_token,
                        chat_id=str(onboarding.max_chat_id),
                        text=ack_text,
                    ),
                    name=f"max_ack:{onboarding.max_chat_id}",
                )
                if settings.ack_edit_max_enabled:
                    ack_progress_controller = MaxAckProgressController(
                        max_client=max_ack_client,
                        ack_message_id_future=ack_task,
                        enabled=True,
                    )

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
                # Не оставляем ack-message в чате если turn оказался
                # ignored — юзер увидит «⏳ Работаю…» и пустоту.
                if ack_task is not None:
                    _create_task(
                        _wait_ack_then_delete(
                            ack_task=ack_task,
                            token=settings.max_bot_token,
                            wait_for_delivery=False,
                            tenant_id=onboarding.tenant_id,
                            turn_started_at=turn_started_at,
                        ),
                        name=f"max_ack_del:{inbound_message_id}",
                    )
                _set_processing_status(
                    bg_session, inbound_message_id, "ignored",
                )
                return

            runtime = ActionRuntimeService(
                bg_session,
                telegram_client=None,
                ack_progress_controller=ack_progress_controller,
            )
            queued = runtime.enqueue_action(action)
            await runtime.process_job(queued.job_id)

            # После main turn — спавним cleanup'у ack message.
            # Polls outbox для tenant_id+since (start of turn), max 15s,
            # потом DELETE /messages. Если delivery failed — ack
            # остаётся (юзер видит «⏳ Работаю…», знает что бот пытался).
            if _should_cleanup_ack_after_runtime(
                ack_task=ack_task,
                ack_progress_controller=ack_progress_controller,
            ):
                _create_task(
                    _wait_ack_then_delete(
                        ack_task=ack_task,
                        token=settings.max_bot_token,
                        wait_for_delivery=True,
                        tenant_id=onboarding.tenant_id,
                        turn_started_at=turn_started_at,
                    ),
                    name=f"max_ack_del:{inbound_message_id}",
                )

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


async def _send_max_ack(
    *, token: str, chat_id: str, text: str,
) -> str | None:
    """Send одну ack-фразу в МАКС, возвращаем mid для последующего delete.

    Mirror ``services.telegram_inbound._fire_and_forget_ack``. Failures
    swallowed — ack это UX sugar, не correctness-critical signal.
    """
    try:
        client = MaxClient(token=token)
        response = await client.send_message(
            recipient={"chat_id": chat_id}, text=text,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("max ack send failed: %s", exc)
        return None
    # Probe 2026-05-05 PM: primary response shape
    # `{"message": {"body": {"mid": ...}, ...}}`. MAX has returned
    # nearby shapes in probes; parse defensively because losing this mid
    # silently degrades ack-edit streaming into a separate final send.
    response_dict = response if isinstance(response, dict) else {}
    msg = response_dict.get("message")
    if not isinstance(msg, dict):
        msg = response_dict
    body = msg.get("body")
    body_dict = body if isinstance(body, dict) else {}
    mid = (
        body_dict.get("mid")
        or body_dict.get("message_id")
        or msg.get("mid")
        or msg.get("message_id")
        or msg.get("id")
        or response_dict.get("mid")
        or response_dict.get("message_id")
        or response_dict.get("id")
    )
    return str(mid) if mid else None


async def _wait_ack_then_delete(
    *,
    ack_task: asyncio.Task,
    token: str,
    wait_for_delivery: bool,
    tenant_id: str,
    turn_started_at: datetime,
) -> None:
    """Wait for ack send to finish, then DELETE the ack message.

    Args:
        ack_task: задача ``_send_max_ack`` — её результат = ack mid (или None).
        wait_for_delivery: True для normal turns — polls outbox row
            делая sure что real reply already delivered (clean-chat UX —
            ack исчезает только когда появляется real reply).
            False для ignored/early-exit turns — delete сразу (нет real
            reply на pending).
        tenant_id: для polling outbox row delivery (per-tenant scoped).
        turn_started_at: cut-off time для поиска outbox row этого turn'а.

    Best-effort throughout. Любая ошибка → log debug + return.
    Timeout 15s overall — если delivery worker завис, ack остаётся.
    """
    try:
        ack_mid = await asyncio.wait_for(asyncio.shield(ack_task), timeout=10.0)
    except asyncio.TimeoutError:
        logger.debug("max ack send timeout — leaving ack visible")
        return
    except Exception:  # noqa: BLE001
        logger.debug("max ack task failed", exc_info=True)
        return
    if not ack_mid:
        return  # ack send failed; nothing to delete

    if wait_for_delivery:
        delivered = await _wait_outbox_delivered_for_tenant(
            tenant_id=tenant_id, since=turn_started_at, timeout_s=15.0,
        )
        if not delivered:
            logger.info(
                "max ack cleanup: outbox not delivered after 15s — "
                "leaving ack mid=%s visible (tenant=%s)",
                ack_mid, tenant_id,
            )
            return

    try:
        client = MaxClient(token=token)
        await client.delete_message(ack_mid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("max ack delete failed (mid=%s): %s", ack_mid, exc)


def _should_cleanup_ack_after_runtime(
    *,
    ack_task: asyncio.Task | None,
    ack_progress_controller: object | None,
) -> bool:
    if ack_task is None:
        return False
    return not bool(getattr(ack_progress_controller, "final_edit_planned", False))


async def _wait_outbox_delivered_for_tenant(
    *, tenant_id: str, since: datetime, timeout_s: float = 15.0,
) -> bool:
    """Poll outbox_messages для tenant'а до status='sent' (channel=max).

    NB: OutboxMessage не имеет inbound_message_id column, поэтому
    correlation by tenant_id + created_at > since (start of turn) +
    channel='max'. В edge cases (overlapping turns) может surface
    false-positive (early ack delete на другой outbox row), но per-tenant
    lock сериализует turn'ы, так что overlap unlikely.

    Returns True если хотя бы одна row 'sent', False если timeout.
    """
    from sqlalchemy import select

    from sreda.db.models.core import OutboxMessage

    SessionLocal = get_session_factory()
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            with SessionLocal() as poll_sess:
                row = poll_sess.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.channel_type == "max",
                        OutboxMessage.status == "sent",
                        OutboxMessage.created_at >= since,
                    ).limit(1)
                ).first()
                if row is not None:
                    return True
        except Exception:  # noqa: BLE001
            logger.debug("max ack outbox poll crashed", exc_info=True)
            return False
        await asyncio.sleep(0.3)
    return False


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

    if data.startswith("persona:") or data.startswith("personaset:"):
        from sreda.services.housewife_persona import (
            PERSONA_SETTINGS_CALLBACK_PREFIX,
            build_persona_selected_keyboard_max,
            build_persona_selected_message,
            set_persona_preset,
        )

        # #130: источник — префиксом (см. TG-ветку)
        _mid_life = data.startswith(PERSONA_SETTINGS_CALLBACK_PREFIX)
        preset = (
            data.removeprefix(PERSONA_SETTINGS_CALLBACK_PREFIX)
            if _mid_life else data.removeprefix("persona:")
        ).strip()
        if callback_id:
            try:
                await max_client.answer_callback(str(callback_id), notification="")
            except Exception:  # noqa: BLE001
                logger.warning(
                    "max persona callback ack failed (data=%r) — continuing",
                    data, exc_info=True,
                )
        if not (onboarding.tenant_id and onboarding.user_id):
            logger.warning("max persona: no tenant/user in callback context")
            return True
        try:
            set_persona_preset(
                session,
                tenant_id=onboarding.tenant_id,
                user_id=onboarding.user_id,
                preset=preset,
                source="user_command",
                actor_user_id=onboarding.user_id,
            )
            session.commit()
        except ValueError:
            session.rollback()
            logger.warning(
                "max persona: unknown preset tenant=%s preset=%r",
                onboarding.tenant_id, preset,
            )
            return True
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("max persona: failed to store preset")
            return True

        msg = payload.get("message")
        cb_message_mid: str | None = None
        if isinstance(msg, dict):
            body = msg.get("body")
            if isinstance(body, dict) and body.get("mid"):
                cb_message_mid = str(body["mid"])

        # #130: смена стиля в середине жизни — короткое подтверждение
        # без онбординг-хвоста и кнопок (зеркало TG-ветки).
        reply_text = build_persona_selected_message(
            preset, in_onboarding=not _mid_life,
        )
        # Codex R1 (оба): None не снимает кнопки в MAX — снятие = [].
        attachments = (
            [] if _mid_life else build_persona_selected_keyboard_max()
        )
        edited = False
        if cb_message_mid:
            try:
                await max_client.edit_message(
                    cb_message_mid,
                    text=reply_text,
                    attachments=attachments,
                )
                edited = True
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "max persona: edit_message failed mid=%s tenant=%s: %s "
                    "— fallback to send_message",
                    cb_message_mid, onboarding.tenant_id, exc,
                )

        if not edited and getattr(onboarding, "max_chat_id", None):
            try:
                await max_client.send_message(
                    recipient={"chat_id": onboarding.max_chat_id},
                    text=reply_text,
                    attachments=attachments,
                )
            except Exception:  # noqa: BLE001
                logger.warning("max persona: delivery failed", exc_info=True)

        return True

    if data == "persona_ready":
        from sreda.services import pending_bot
        from sreda.services.housewife_onboarding import record_pb_tour_progress

        if callback_id:
            try:
                await max_client.answer_callback(str(callback_id), notification="")
            except Exception:  # noqa: BLE001
                logger.warning(
                    "max persona_ready callback ack failed — continuing",
                    exc_info=True,
                )

        msg = payload.get("message")
        cb_message_mid: str | None = None
        if isinstance(msg, dict):
            body = msg.get("body")
            if isinstance(body, dict) and body.get("mid"):
                cb_message_mid = str(body["mid"])

        reply_text = pending_bot.done_broadcast_reply().text
        delivered = False
        if cb_message_mid:
            try:
                await max_client.edit_message(
                    cb_message_mid,
                    text=reply_text,
                    attachments=[],
                )
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "max persona_ready: edit_message failed mid=%s tenant=%s: %s "
                    "— fallback to send_message",
                    cb_message_mid, onboarding.tenant_id, exc,
                )

        if not delivered and getattr(onboarding, "max_chat_id", None):
            try:
                await max_client.send_message(
                    recipient={"chat_id": onboarding.max_chat_id},
                    text=reply_text,
                    attachments=[],
                )
                delivered = True
            except Exception:  # noqa: BLE001
                logger.warning("max persona_ready: delivery failed", exc_info=True)

        if delivered and onboarding.tenant_id and onboarding.user_id:
            try:
                record_pb_tour_progress(
                    session,
                    tenant_id=onboarding.tenant_id,
                    user_id=onboarding.user_id,
                    branch="done",
                )
                session.commit()
            except Exception:  # noqa: BLE001
                session.rollback()
                logger.exception("max persona_ready: progress tracking failed")

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


async def _handle_max_persona_settings_request(
    *,
    max_client,
    onboarding,
) -> None:
    """Send persona choice keyboard for deterministic MAX settings intent."""
    if not getattr(onboarding, "max_chat_id", None):
        logger.warning(
            "max persona settings: no chat_id tenant=%s",
            getattr(onboarding, "tenant_id", None),
        )
        return

    from sreda.services.housewife_persona import (
        build_persona_choice_keyboard_max,
        build_persona_choice_message,
    )

    try:
        await max_client.send_message(
            recipient={"chat_id": onboarding.max_chat_id},
            text=build_persona_choice_message(),
            attachments=build_persona_choice_keyboard_max(settings=True),
        )
    except Exception:  # noqa: BLE001
        logger.warning("max persona settings: delivery failed", exc_info=True)


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


async def _handle_max_link_start_cmd(*, raw_token: str, chat_id: str | None) -> None:
    """Handle `/start lnk_<token>` command in MAX bot chat.

    Replies с inline button «✅ Подтвердить» / «❌ Отмена». User tap
    triggers `confirm_link:<token>` или `cancel_link:<token>` callback
    что мы catches in handle_max_update.
    """
    from sreda.services.channel_linking import lookup_token

    settings = get_settings()
    if not (settings.max_bot_token and chat_id):
        logger.warning("max link /start: missing max_bot_token или chat_id")
        return

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        token_row = lookup_token(session, raw_token)

    client = MaxClient(token=settings.max_bot_token)

    if token_row is None:
        try:
            await client.send_message(
                recipient={"chat_id": chat_id},
                text="Ссылка истекла или уже использована. Сгенерируй новую в Telegram.",
            )
        except Exception:  # noqa: BLE001
            logger.warning("max link /start invalid token reply failed", exc_info=True)
        return

    if token_row.target_channel != "max":
        try:
            await client.send_message(
                recipient={"chat_id": chat_id},
                text="Эта ссылка не для MAX. Открой её в Telegram.",
            )
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        await client.send_message(
            recipient={"chat_id": chat_id},
            text=(
                "Подтвердить связь твоего MAX-аккаунта с Sreda юзером?\n\n"
                "⚠ Если у тебя был отдельный аккаунт Среды в MAX — его данные "
                "будут заменены."
            ),
            attachments=[{
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [[
                        {"type": "callback", "text": "✅ Подтвердить",
                         "payload": f"confirm_link:{raw_token}"},
                        {"type": "callback", "text": "❌ Отмена",
                         "payload": f"cancel_link:{raw_token}"},
                    ]],
                },
            }],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("max link /start confirm send failed: %s", exc)


async def _handle_max_link_confirm_cb(
    *, raw_token: str, sender_user_id: str, chat_id: str | None,
    callback_id,
) -> None:
    """Handle `confirm_link:<token>` callback — execute consume_link."""
    from sreda.services.channel_linking import consume_link

    settings = get_settings()
    SessionLocal = get_session_factory()

    with SessionLocal() as session:
        outcome = consume_link(
            session,
            raw_token=raw_token,
            target_channel="max",
            target_account_id=sender_user_id,
            target_chat_id=chat_id,
        )

    if outcome.success:
        reply_text = (
            "✅ Аккаунты связаны! Теперь Среда видит тебя в обоих мессенджерах."
        )
    elif outcome.error == "account_already_registered_separately":
        reply_text = (
            "У тебя уже есть отдельный Sreda-аккаунт в MAX. "
            "Напиши в @sreda_support — свяжем вручную."
        )
    elif outcome.error == "already_linked_other_account":
        reply_text = "К этому Sreda-юзеру уже привязан другой MAX-аккаунт."
    elif outcome.error == "account_belongs_to_other_family_member":
        reply_text = "Этот аккаунт уже привязан к другому члену семьи."
    elif outcome.error == "not_found_or_expired":
        reply_text = "Ссылка истекла или уже использована."
    else:
        reply_text = f"Не удалось связать: {outcome.error or 'неизвестная ошибка'}"

    if not settings.max_bot_token:
        return
    client = MaxClient(token=settings.max_bot_token)
    try:
        if callback_id:
            await client.answer_callback(
                str(callback_id),
                message={"text": reply_text, "attachments": []},
            )
        elif chat_id:
            await client.send_message(
                recipient={"chat_id": chat_id}, text=reply_text,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("max link confirm reply failed: %s", exc)


async def _handle_max_link_cancel_cb(
    *, raw_token: str, callback_id,
) -> None:
    """Handle `cancel_link:<token>` callback — invalidate token."""
    from sreda.db.models.channel_linking import ChannelLinkToken
    from sreda.services.channel_linking import _hash_token
    from sqlalchemy import update as sa_update

    settings = get_settings()
    SessionLocal = get_session_factory()

    with SessionLocal() as session:
        token_hash = _hash_token(raw_token)
        session.execute(
            sa_update(ChannelLinkToken)
            .where(
                ChannelLinkToken.token_hash == token_hash,
                ChannelLinkToken.used_at.is_(None),
            )
            .values(used_at=datetime.now(timezone.utc))
        )
        session.commit()

    if not (settings.max_bot_token and callback_id):
        return
    client = MaxClient(token=settings.max_bot_token)
    try:
        await client.answer_callback(
            str(callback_id),
            message={"text": "Отменено. Ссылка деактивирована.", "attachments": []},
        )
    except Exception:  # noqa: BLE001
        pass


async def _handle_max_pending_tenant(
    *, session, payload: dict, update_type: str | None, onboarding,
    settings, is_post_approve_tour: bool = False,
) -> None:
    """Send pending welcome message via MaxClient.

    Mirror TG ``_handle_pending_tenant``:
    - bot_started: send first tour screen (pending_bot.match(None)) with buttons
    - message_callback с payload "pb:*": **edit** original message
      in place (mirror TG editMessageText flow), fallback to send.
      Also acks the callback to remove "loading" UX.
    - message_created: silent (юзер видел первый экран; spam'инг неуместно)
    - другое: silent

    Errors swallowed — pending welcome это UX sugar, не correctness-critical.

    ``is_post_approve_tour`` — set True когда caller routed approved юзера
    с pb:* callback'ом. На pb:done показывает вопрос имени и ставит
    waiting-flag. Для truly pending юзеров (False) показывает closing
    без вопроса имени: pending text не должен сохраняться как имя.
    """
    if not (settings.max_bot_token and onboarding.max_chat_id):
        logger.info(
            "max pending: no token/chat_id — drop tenant=%s",
            onboarding.tenant_id,
        )
        return

    from sreda.integrations.max.client import (
        MaxClient, render_max_inline_keyboard_attachment,
    )
    from sreda.services import pending_bot

    input_text: str | None = None
    is_callback = False
    callback_id: str | None = None
    cb_message_mid: str | None = None

    if update_type == "bot_started":
        # First branch — pending_bot.match(None) returns voice reply
        pass  # input_text=None, is_callback=False
    elif update_type == "message_callback":
        callback = payload.get("callback") or {}
        cb_data = callback.get("payload") or ""
        if isinstance(cb_data, str) and pending_bot.is_pending_callback(cb_data):
            input_text = cb_data
            is_callback = True
            cb_id_raw = callback.get("callback_id")
            if cb_id_raw:
                callback_id = str(cb_id_raw)
            # MAX: original message (с inline keyboard) живёт в
            # ``payload.message.body.mid`` для message_callback events.
            msg = payload.get("message")
            if isinstance(msg, dict):
                body = msg.get("body")
                if isinstance(body, dict):
                    mid = body.get("mid")
                    if mid:
                        cb_message_mid = str(mid)
        else:
            return  # not pending_bot button → silent
    else:
        return  # message_created / other → silent

    # Determine current branch for navigation keyboard.
    from sreda.services.pending_bot import _BRANCHES as _PB_BRANCHES
    current_branch = "voice"
    if is_callback and input_text:
        raw = input_text.removeprefix("pb:").strip()
        if raw in _PB_BRANCHES:
            current_branch = raw
    if is_post_approve_tour and current_branch == "done":
        reply = pending_bot.done_broadcast_reply()
    else:
        reply = pending_bot.match(input_text, is_callback=is_callback)
    keyboard = pending_bot.build_navigation_keyboard(current_branch)
    attachments = render_max_inline_keyboard_attachment(keyboard) if keyboard else None

    client = MaxClient(token=settings.max_bot_token)

    edited = False
    if is_callback and cb_message_mid:
        try:
            await client.edit_message(
                cb_message_mid,
                text=reply.text,
                attachments=attachments or [],
            )
            edited = True
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "max pending: edit_message failed mid=%s tenant=%s: %s "
                "— fallback to send_message",
                cb_message_mid, onboarding.tenant_id, exc,
            )

    if not edited:
        try:
            await client.send_message(
                recipient={"chat_id": onboarding.max_chat_id},
                text=reply.text,
                attachments=attachments,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "max pending welcome send failed tenant=%s: %s",
                onboarding.tenant_id, exc,
            )

    # Ack callback — remove «loading» state в UI юзера.
    if is_callback and callback_id:
        try:
            await client.answer_callback(callback_id)
        except Exception:  # noqa: BLE001
            pass  # ack failure не critical — ux sugar

    if (
        is_post_approve_tour
        and is_callback
        and onboarding.tenant_id
        and onboarding.user_id
    ):
        try:
            from sreda.services.housewife_onboarding import (
                record_pb_tour_progress,
            )

            record_pb_tour_progress(
                session,
                tenant_id=onboarding.tenant_id,
                user_id=onboarding.user_id,
                branch=current_branch,
            )
            session.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "max pb: progress tracking failed for branch=%s",
                current_branch,
            )
            session.rollback()

    # 2026-05-09 (Boris feedback): post-tour name prompt теперь часть
    # done text inline (см. _DONE в pending_bot.py). Отдельный
    # follow-up message больше не шлём — снижаем noise (один message
    # vs два). is_user_named idempotency helpers сохранены для
    # будущей фичи «conditional name prompt» если потребуется.


def _set_processing_status(session, inbound_message_id: str, status: str) -> None:
    """Update inbound_messages.processing_status + commit."""
    from sreda.db.models.core import InboundMessage

    row = session.get(InboundMessage, inbound_message_id)
    if row is not None:
        row.processing_status = status
        session.commit()


# R-19 (2026-05-13): known MAX callback prefixes that our handlers know
# how to route. Used for sync detection of unknown-prefix callbacks
# (echo events from MAX, stale tokens, etc.) → drop with status=ignored
# instead of spawning a background task that may silently fail to set
# processing_status (causing monitor stuck-inbound alerts).
_KNOWN_MAX_CALLBACK_PREFIXES: tuple[str, ...] = (
    "btn_reply:",   # inline-button label reply (our token-based)
    "persona:",     # housewife persona preset choice
    "pb:",          # pending_bot tour navigation
    "rem_done:",    # reminder mark done
    "rem_snooze:",  # reminder snooze
    "confirm_link:",  # channel-link confirmation (handled earlier)
    "cancel_link:",   # channel-link cancellation (handled earlier)
)

_KNOWN_MAX_CALLBACK_EXACT: frozenset[str] = frozenset({
    "persona_ready",  # housewife persona onboarding skip
})


def _max_callback_payload(payload: dict) -> str | None:
    """Extract `callback.payload` string from MAX update. Returns None if
    not a message_callback или payload format unexpected."""
    if payload.get("update_type") != "message_callback":
        return None
    callback = payload.get("callback")
    if not isinstance(callback, dict):
        return None
    cb_data = callback.get("payload")
    return cb_data if isinstance(cb_data, str) else None


def _is_unknown_max_callback_prefix(payload: dict) -> bool:
    """True если payload — message_callback с prefix'ом, который не
    routится ни одним из наших handler'ов. Для таких callback'ов
    (MAX system-generated echo, stale tokens, etc.) синхронный drop
    безопаснее spawn'а background task'а.

    False для НЕ message_callback (другие update types should pass through).
    """
    if payload.get("update_type") != "message_callback":
        return False
    cb_data = _max_callback_payload(payload)
    if cb_data is None:
        # message_callback но payload не строка / отсутствует → unknown
        return True
    if cb_data in _KNOWN_MAX_CALLBACK_EXACT:
        return False
    return not any(
        cb_data.startswith(p) for p in _KNOWN_MAX_CALLBACK_PREFIXES
    )


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
