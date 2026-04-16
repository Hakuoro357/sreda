from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.features.app_registry import get_feature_registry
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.dispatcher import dispatch_telegram_action
from sreda.runtime.executor import ActionRuntimeService
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

    # 1. Feature registered?
    registry = get_feature_registry()
    if _VOICE_FEATURE_KEY not in registry.modules:
        await _send_error(
            "Я пока не умею обрабатывать голосовые сообщения. "
            "Подключите навык распознавания речи → /subscriptions"
        )
        return None

    # 2. Quota check
    budget = BudgetService(session)
    if not tenant_id or not budget.has_quota(tenant_id, _VOICE_FEATURE_KEY):
        await _send_error(
            "Лимит распознавания голоса исчерпан. Проверьте подписку → /subscriptions"
        )
        return None

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

    # 5. Download audio
    file_id = voice.get("file_id")
    if not file_id:
        await _send_error("Не удалось получить голосовое сообщение. Попробуйте ещё раз.")
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
        return None

    # 6. Transcribe
    try:
        text = await recognizer.recognize(audio_bytes)
    except SpeechRecognitionError as exc:
        logger.warning("Speech recognition failed: %s", exc)
        await _send_error("Не удалось расшифровать сообщение. Попробуйте ещё раз.")
        return None

    # 7. Record usage (1 credit per message)
    budget.record_api_usage(
        tenant_id=tenant_id,
        feature_key=_VOICE_FEATURE_KEY,
        provider_key=settings.speech_provider or "unknown",
        task_type="speech_recognition",
        credits_consumed=1,
    )

    # 8. Send transcription back to user and stop processing.
    # Once a chat-capable skill is available, this block should be replaced
    # with ``message["text"] = text; return payload`` to route transcribed
    # text into the normal pipeline.
    try:
        await telegram_client.send_message(
            chat_id=chat_id,
            text=f"🎤 {text}",
        )
    except TelegramDeliveryError as exc:
        logger.warning("Failed to send transcription: %s", exc)
    return None


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
