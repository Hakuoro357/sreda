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
from sreda.services.onboarding import TelegramOnboardingResult, build_welcome_message
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

    if onboarding.is_new_user:
        text, reply_markup = build_welcome_message()
        await telegram_client.send_message(
            chat_id=onboarding.chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        return

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
            "Я пока не умею обрабатывать голосовые сообщения. "
            "Подключите подходящий агент → /subscriptions"
        )
        return None

    # 2. Tenant has an active agent that includes voice?
    # Voice is no longer a standalone subscription — it's a capability
    # bundled with agents like Помощник домохозяйки (see manifest
    # ``includes_voice``). EDS Monitor and other text-only agents do
    # NOT grant voice.
    if not tenant_id or not has_voice_access(session, tenant_id):
        await _send_error(
            "Распознавание голосовых сообщений включено в Помощник "
            "домохозяйки. Подключите его → /subscriptions"
        )
        return None
    budget = BudgetService(session)

    # 3. Duration limit
    duration = voice.get("duration", 0)
    if duration > _VOICE_MAX_DURATION_SECONDS:
        await _send_error(
            f"Голосовое сообщение слишком длинное (макс. {_VOICE_MAX_DURATION_SECONDS} секунд). "
            "Попробуйте короче."
        )
        return None

    # 4. Speech provider configured?
    settings = get_settings()
    recognizer = get_speech_recognizer(settings)
    if recognizer is None:
        await _send_error("Сервис распознавания речи не настроен. Обратитесь к администратору.")
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
            await _send_error("Не удалось получить голосовое сообщение. Попробуйте ещё раз.")
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
            await _send_error("Не удалось получить голосовое сообщение. Попробуйте ещё раз.")
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
            await _send_error("Не удалось расшифровать сообщение. Попробуйте ещё раз.")
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
                    callback_id, text="Напоминание уже закрыто"
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
