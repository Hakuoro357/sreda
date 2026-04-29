from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.features.app_registry import get_feature_registry
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.dispatcher import dispatch_telegram_action
from sreda.runtime.executor import ActionRuntimeService
from sreda.services import trace
from sreda.services.agent_capabilities import has_voice_access
from sreda.services.budget import BudgetService
from sreda.services.onboarding import TelegramOnboardingResult
from sreda.services.speech.base import SpeechRecognitionError
from sreda.services.speech.factory import get_speech_recognizer

logger = logging.getLogger(__name__)

_VOICE_FEATURE_KEY = "voice_transcription"
_VOICE_MAX_DURATION_SECONDS = 30


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

    # 2026-04-27 simplified: ветка `is_new_user` удалена. До approval-gate
    # юзер не доходит до этой функции (webhook routes pending tenants
    # в pending_bot.match). После approve admin route шлёт
    # build_post_approve_message напрямую — отдельной ветки тут не надо.

    payload = await _maybe_transcribe_voice(
        payload,
        session=session,
        telegram_client=telegram_client,
        onboarding=onboarding,
    )
    if payload is None:
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


async def _maybe_transcribe_voice(
    payload: dict,
    *,
    session: Session,
    telegram_client: TelegramClient,
    onboarding: TelegramOnboardingResult,
) -> dict | None:
    """If the payload contains a voice message, transcribe it and inject the
    text into ``payload["message"]["text"]``. Returns the (possibly mutated)
    payload on success, or None if an error message was sent to the user and
    processing should stop."""
    message = payload.get("message")
    if not isinstance(message, dict):
        return payload
    voice = message.get("voice")
    if not isinstance(voice, dict):
        # Not a voice message — nothing to do.
        return payload

    chat_id = onboarding.chat_id
    tenant_id = onboarding.tenant_id

    async def _send_error(text: str) -> None:
        try:
            await telegram_client.send_message(chat_id=chat_id, text=text)
        except TelegramDeliveryError as exc:
            logger.warning("Failed to send voice error message: %s", exc)

    # 1. Voice plugin installed? Runtime package must be present — otherwise
    # the Yandex SpeechKit dependency is missing and we can't transcribe
    # regardless of what agent the user has.
    registry = get_feature_registry()
    if _VOICE_FEATURE_KEY not in registry.modules:
        await _send_error(
            "Голосовые сообщения доступны в подписке. "
            "Открой /subscriptions, чтобы узнать подробнее."
        )
        return None

    # 2. Tenant has an active agent that includes voice?
    # Voice is no longer a standalone subscription — it's a capability
    # bundled with agents like Помощник домохозяйки (see manifest
    # ``includes_voice``). EDS Monitor and other text-only agents do
    # NOT grant voice.
    if not tenant_id or not has_voice_access(session, tenant_id):
        await _send_error(
            "Голосовые сообщения доступны в подписке. "
            "Открой /subscriptions, чтобы узнать подробнее."
        )
        return None
    budget = BudgetService(session)

    # 3. Duration limit
    duration = voice.get("duration", 0)
    if duration > _VOICE_MAX_DURATION_SECONDS:
        await _send_error(
            f"Голосовое сообщение слишком длинное — "
            f"макс. {_VOICE_MAX_DURATION_SECONDS} секунд. Отправь покороче."
        )
        return None

    # 4. Speech provider configured?
    settings = get_settings()
    recognizer = get_speech_recognizer(settings)
    if recognizer is None:
        await _send_error("Голосовые сообщения сейчас не работают. Напиши текстом или попробуй позже.")
        return None

    # 5 + 6: Download audio + transcribe. Split across two trace
    # steps as of 2026-04-22 — pproxy → VDS tunnel round-trip on the
    # Telegram ``getFile`` + ``download_file`` calls is comparable to
    # the STT call itself, so rolling them together was hiding whether
    # a slow voice turn was the STT provider's fault or Telegram-side
    # latency. Ops dashboards now get ``voice.download`` vs
    # ``voice.transcribe`` independently.
    provider = settings.speech_provider or "unknown"
    with trace.step("voice.download", provider="telegram") as _dl_meta:
        file_id = voice.get("file_id")
        if not file_id:
            await _send_error("Не удалось получить голосовое сообщение. Отправь ещё раз.")
            _dl_meta["status"] = "no_file_id"
            return None

        try:
            file_info = await telegram_client.get_file_info(str(file_id))
            file_path = file_info.get("file_path")
            if not file_path:
                raise TelegramDeliveryError("file_path missing in getFile response")
            audio_bytes = await telegram_client.download_file(str(file_path))
        except TelegramDeliveryError as exc:
            logger.warning("Voice download failed: %s", exc)
            await _send_error("Не удалось получить голосовое сообщение. Отправь ещё раз.")
            _dl_meta["status"] = "download_failed"
            return None

        _dl_meta["bytes_in"] = len(audio_bytes)

    with trace.step("voice.transcribe", provider=provider) as _trace_meta:
        _trace_meta["bytes_in"] = len(audio_bytes)

        # 6. Transcribe
        try:
            text = await recognizer.recognize(audio_bytes)
        except SpeechRecognitionError as exc:
            logger.warning("Speech recognition failed: %s", exc)
            await _send_error("Не получилось расшифровать голос. Отправь ещё раз или напиши.")
            _trace_meta["status"] = "recognize_failed"
            return None

        _trace_meta["chars_out"] = len(text)
        _trace_meta["status"] = "ok"

    # 7. Record usage (1 credit per message)
    budget.record_api_usage(
        tenant_id=tenant_id,
        feature_key=_VOICE_FEATURE_KEY,
        provider_key=settings.speech_provider or "unknown",
        task_type="speech_recognition",
        credits_consumed=1,
    )

    # 8. Inject transcript into the payload as if the user had typed it.
    # Downstream pipeline (``_extract_message_text`` → dispatcher →
    # ``conversation.chat`` → chat-capable skill) handles it from here.
    # No ``🎤`` echo — the goal is "treated like hand-typed text";
    # Telegram already shows the original voice bubble in chat, so the
    # user can replay it if the transcription looked off.
    message["text"] = text
    return payload


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
    data = str(callback_query.get("data") or "")

    # Reminder-escalation callbacks (v1.2). The housewife reminder
    # worker sends inline buttons with callback_data of the form
    # ``rem_done:<reminder_id>`` / ``rem_snooze:<reminder_id>``. We
    # intercept here BEFORE the generic dispatch so the core runtime
    # pipeline doesn't try to treat this as a chat turn.
    if data.startswith("rem_done:") or data.startswith("rem_snooze:"):
        await _handle_reminder_callback(
            session=session,
            telegram_client=telegram_client,
            callback_query=callback_query,
            data=data,
        )
        return

    # Pending-bot tour callbacks (`pb:<branch>`). 2026-04-28: эти кнопки
    # используются ДВАЖДЫ. (1) В pending-фазе (до approve) — обрабатываются
    # в `telegram_webhook.py` ДО approval-gate. (2) После approve, во
    # время одноразовой broadcast-рассылки (`scripts/broadcast_welcome_v2.py`),
    # юзер уже approved, но получает intro-сообщение и тапает кнопки —
    # callback идёт сюда. В обоих случаях логика та же: матчим branch
    # через `pending_bot.match()`, отправляем следующее сообщение цепочки.
    # Ничего не идёт в LLM, никаких tool-call'ов, чистый scripted-flow.
    if data.startswith("pb:"):
        from datetime import datetime, timedelta, timezone
        from sreda.services import pending_bot
        from sreda.services.housewife_onboarding import (
            WELCOME_V2_PROGRESS_KEY,
            record_pb_tour_progress,
        )
        from sreda.db.repositories.user_profile import UserProfileRepository

        branch = data[len("pb:"):]

        # 2026-04-28 step 1: Idempotency + cooldown.
        # (a) Idempotency: если юзер уже клик'ал кнопку с таким branch'ом
        #     ИЛИ продвинулся дальше по туру — current click это no-op.
        #     "tap N times = 1 click" (по запросу юзера).
        # (b) Cooldown 3s: даже если branch новый, не позволяем больше
        #     1 успешного pb: callback per 3 секунды (защита от tap-flood
        #     на edge cases типа aliases).
        # Защищает от incident'a tg_1089832184 19:25 — 361 callback
        # за 6 минут на одну и ту же кнопку pb:schedule.
        from sreda.services import pending_bot as _pb_mod
        if onboarding.tenant_id and onboarding.user_id:
            try:
                repo = UserProfileRepository(session)
                cfg = repo.get_skill_config(
                    onboarding.tenant_id, onboarding.user_id,
                    "housewife_assistant",
                )
                if cfg is not None:
                    params = UserProfileRepository.decode_skill_params(cfg)
                    progress = params.get(WELCOME_V2_PROGRESS_KEY) or {}
                    if isinstance(progress, dict):
                        # (a) Idempotency: блокируем ТОЛЬКО точный повтор
                        # того же branch'а. С 2026-04-29 wizard стал
                        # двусторонним (prev/next): юзер может валидно
                        # тапнуть «← Голос» когда `last_branch=schedule`,
                        # cur_idx < last_idx, и это не spam. Раньше
                        # `cur_idx <= last_idx` блокировал этот кейс.
                        # Теперь блокируем только cur_idx == last_idx
                        # (аккуратно: спам-loop был на ПОВТОРНЫЕ
                        # `pb:schedule` после schedule, не на «откат
                        # назад на intro»).
                        last_branch = progress.get("last_branch")
                        if last_branch and branch == last_branch:
                            logger.info(
                                "pb: idempotent skip (same branch) tenant=%s branch=%s",
                                onboarding.tenant_id, branch,
                            )
                            if callback_id:
                                try:
                                    await telegram_client.answer_callback_query(
                                        str(callback_id), text=""
                                    )
                                except TelegramDeliveryError:
                                    pass
                            return
                        # (b) Cooldown 3s — fallback для случая когда
                        # last_branch не помогает (напр. первый click)
                        last_at_str = progress.get("last_at")
                        if last_at_str:
                            last_at = datetime.fromisoformat(last_at_str)
                            elapsed = datetime.now(timezone.utc) - last_at
                            if elapsed < timedelta(seconds=3):
                                logger.info(
                                    "pb: cooldown skip tenant=%s branch=%s elapsed=%.2fs",
                                    onboarding.tenant_id, branch, elapsed.total_seconds(),
                                )
                                if callback_id:
                                    try:
                                        await telegram_client.answer_callback_query(
                                            str(callback_id), text=""
                                        )
                                    except TelegramDeliveryError:
                                        pass
                                return
            except Exception:  # noqa: BLE001
                logger.exception("pb: cooldown/idempotency check failed (continuing)")

        # 2026-04-28 step 2: answerCallbackQuery first — если 400
        # (callback query expired, ~15 мин лимит Telegram'а), ВЫХОДИМ
        # БЕЗ отправки следующего сообщения. Юзер уже не на той кнопке,
        # и Telegram redeliver'ит этот update в loop пока answerCB не
        # успешен. Single-attempt — без retry для callback'ов
        # (TelegramClient уже не retry-ит 4xx).
        if callback_id:
            try:
                await telegram_client.answer_callback_query(str(callback_id), text="")
            except TelegramDeliveryError as exc:
                if exc.status_code == 400:
                    # Expired callback — drop entire request silently.
                    logger.info(
                        "pb: callback expired (400) tenant=%s branch=%s — drop",
                        onboarding.tenant_id, branch,
                    )
                    return
                # Other errors (network/5xx): log and continue (можем
                # отправить сообщение даже если ack не прошёл).
                logger.warning(
                    "pb: callback ack failed status=%s: %s",
                    exc.status_code, exc,
                )

        if onboarding.chat_id:
            if branch == "done":
                reply = pending_bot._DONE_BROADCAST
            else:
                reply = pending_bot.match(data, is_callback=True)
            keyboard = pending_bot.build_navigation_keyboard(branch)

            # 2026-04-29: edit-based wizard. Вместо send_message нового
            # сообщения per branch — editMessageText того же
            # сообщения с новым text + новой keyboard. Юзер видит
            # plавный wizard с prev/next, а не 11 сообщений в чате.
            # message_id берём из callback_query (Telegram включает
            # сообщение к которому привязана нажатая кнопка).
            cb_message = (
                callback_query.get("message")
                if isinstance(callback_query, dict) else None
            )
            msg_id = (
                cb_message.get("message_id")
                if isinstance(cb_message, dict) else None
            )

            edited = False
            if msg_id is not None:
                try:
                    await telegram_client.edit_message_text(
                        chat_id=str(onboarding.chat_id),
                        message_id=int(msg_id),
                        text=reply.text,
                        reply_markup=keyboard,
                    )
                    edited = True
                except TelegramDeliveryError as exc:
                    # 400 «message is not modified» — Telegram молча
                    # игнорировать (юзер тапнул на ту же ветку, но
                    # её мы уже отфильтровали idempotency-проверкой
                    # выше; это запасной guard для гонок).
                    # 400 «message to edit not found» / 403 «bot blocked» —
                    # fallback на send_message ниже.
                    if exc.status_code == 400 and "not modified" in (str(exc) or "").lower():
                        edited = True  # silent no-op
                    else:
                        logger.info(
                            "pb: editMessageText failed status=%s — fallback to send_message",
                            exc.status_code,
                        )

            if not edited:
                try:
                    await telegram_client.send_message(
                        chat_id=onboarding.chat_id,
                        text=reply.text,
                        reply_markup=keyboard,
                    )
                except TelegramDeliveryError as exc:
                    # 429 — rate-limited, retries только усугубят. Просто
                    # log + drop. 403 — bot blocked. 400 — bad chat_id.
                    # Все 4xx — non-retryable.
                    logger.warning(
                        "pb: branch '%s' delivery failed status=%s: %s",
                        branch, exc.status_code, exc,
                    )

        # Трекаем прогресс welcome v2 тура. Best-effort.
        if onboarding.tenant_id and onboarding.user_id:
            try:
                record_pb_tour_progress(
                    session,
                    tenant_id=onboarding.tenant_id,
                    user_id=onboarding.user_id,
                    branch=branch,
                )
                session.commit()
            except Exception:  # noqa: BLE001
                logger.exception("pb: progress tracking failed for branch=%s", branch)
                session.rollback()
        return

    # Inline-кнопки (Часть 0 плана v2). LLM в прошлом turn'е положил
    # в БД токены для 2-4 кнопок; payload у них = "btn_reply:<token>".
    # Достаём label по токену, подставляем как message.text и дальше —
    # обычный chat-turn, будто юзер сам это написал.
    if data.startswith("btn_reply:"):
        await _handle_btn_reply_callback(
            session=session,
            telegram_client=telegram_client,
            callback_query=callback_query,
            onboarding=onboarding,
            bot_key=bot_key,
            payload=payload,
            inbound_message_id=inbound_message_id,
            token=data[len("btn_reply:"):],
        )
        return

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


async def _handle_reminder_callback(
    *,
    session: Session,
    telegram_client: TelegramClient,
    callback_query: dict,
    data: str,
) -> None:
    """Handle the "Сделал ✅" / "Отложить ⏰" buttons on a housewife
    reminder message. Updates the FamilyReminder state, edits the
    original message to remove the keyboard (so the user can't tap
    twice) and answers the callback with a short toast.
    """
    from sreda.db.models.housewife import FamilyReminder
    from sreda.services.housewife_reminders import (
        SNOOZE_DEFAULT_MINUTES,
        HousewifeReminderService,
    )

    action, _, reminder_id = data.partition(":")
    callback_id = str(callback_query.get("id") or "")
    message = callback_query.get("message") or {}
    chat = (message.get("chat") or {}) if isinstance(message, dict) else {}
    chat_id = str(chat.get("id") or "") if isinstance(chat, dict) else ""
    message_id = message.get("message_id") if isinstance(message, dict) else None
    original_text = (message.get("text") or "") if isinstance(message, dict) else ""

    reminder = session.get(FamilyReminder, reminder_id) if reminder_id else None
    if reminder is None:
        if callback_id:
            try:
                await telegram_client.answer_callback_query(
                    callback_id, text="Это напоминание уже выполнено."
                )
            except TelegramDeliveryError:
                pass
        return

    service = HousewifeReminderService(session)
    toast_text: str
    new_message_text: str
    if action == "rem_done":
        service.acknowledge(reminder)
        toast_text = "Принято ✅"
        # Replace the bell emoji with a check so the user sees the
        # ack state at a glance when scrolling chat history.
        new_message_text = "✅ " + original_text.lstrip("🔔 ").strip()
    else:  # rem_snooze
        service.snooze(reminder, minutes=SNOOZE_DEFAULT_MINUTES)
        toast_text = f"Отложено на {SNOOZE_DEFAULT_MINUTES} мин ⏰"
        new_message_text = (
            f"⏰ {original_text.lstrip('🔔 ').strip()} "
            f"(напомню через {SNOOZE_DEFAULT_MINUTES} мин)"
        )
    session.commit()

    if callback_id:
        try:
            await telegram_client.answer_callback_query(callback_id, text=toast_text)
        except TelegramDeliveryError as exc:
            logger.warning("reminder callback ack failed: %s", exc)

    # Clear the inline keyboard — pass an empty inline_keyboard rather
    # than None so editMessageText actually strips the buttons (Telegram
    # Bot API: omit reply_markup to leave unchanged).
    if chat_id and message_id:
        try:
            await telegram_client.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=new_message_text,
                reply_markup={"inline_keyboard": []},
            )
        except TelegramDeliveryError as exc:
            logger.warning("reminder edit_message_text failed: %s", exc)


async def _handle_btn_reply_callback(
    *,
    session: Session,
    telegram_client: TelegramClient,
    callback_query: dict,
    onboarding: TelegramOnboardingResult,
    bot_key: str,
    payload: dict,
    inbound_message_id: str | None,
    token: str,
) -> None:
    """Inline-кнопка от LLM-ответа (Часть 0 плана v2).

    Resolve токена в label, подставляем label как text-message и
    запускаем обычный chat-turn, будто юзер сам это написал.
    Если токен устарел / уже использован / чужой — toast-отлуп.
    Клавиатуру под старым сообщением стираем.
    """
    from sreda.runtime.executor import ActionRuntimeService
    from sreda.services.reply_buttons import ReplyButtonService

    callback_id = str(callback_query.get("id") or "")
    message = callback_query.get("message") or {}
    chat = (message.get("chat") or {}) if isinstance(message, dict) else {}
    chat_id = str(chat.get("id") or "") if isinstance(chat, dict) else ""
    message_id = message.get("message_id") if isinstance(message, dict) else None
    original_text = (message.get("text") or "") if isinstance(message, dict) else ""

    label: str | None = None
    if onboarding.tenant_id and onboarding.user_id:
        label = ReplyButtonService(session).resolve_token(
            tenant_id=onboarding.tenant_id,
            user_id=onboarding.user_id,
            token=token,
        )

    if label is None:
        if callback_id:
            try:
                await telegram_client.answer_callback_query(
                    callback_id, text="Выбор устарел. Напиши что нужно."
                )
            except TelegramDeliveryError:
                pass
        return

    # Toast + убираем клавиатуру, чтобы повторный клик не работал.
    if callback_id:
        try:
            await telegram_client.answer_callback_query(callback_id, text=label)
        except TelegramDeliveryError as exc:
            logger.warning("btn_reply callback ack failed: %s", exc)
    if chat_id and message_id:
        try:
            await telegram_client.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=original_text,  # оставляем текст как есть
                reply_markup={"inline_keyboard": []},
            )
        except TelegramDeliveryError as exc:
            logger.warning("btn_reply edit_message_text failed: %s", exc)

    # Подставляем label как обычный text-message и запускаем chat-turn.
    synthetic_payload = dict(payload)
    synthetic_payload.pop("callback_query", None)
    synthetic_message = dict(message) if isinstance(message, dict) else {}
    synthetic_message["text"] = label
    synthetic_payload["message"] = synthetic_message

    runtime_action = dispatch_telegram_action(
        payload=synthetic_payload,
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
