"""Unit tests for voice transcription preprocessing (_maybe_transcribe_voice)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sreda.services.speech.base import SpeechRecognitionError
from sreda.services.telegram_bot import _maybe_transcribe_voice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_voice_payload(duration: int = 10, file_id: str = "file123") -> dict:
    return {"message": {"voice": {"duration": duration, "file_id": file_id}}}


def _make_onboarding(tenant_id: str = "t1", chat_id: str = "chat1"):
    onboarding = MagicMock()
    onboarding.tenant_id = tenant_id
    onboarding.chat_id = chat_id
    return onboarding


def _make_telegram(file_path: str = "voice/file.oga", audio: bytes = b"audio"):
    client = MagicMock()
    client.get_file_info = AsyncMock(return_value={"file_path": file_path})
    client.download_file = AsyncMock(return_value=audio)
    client.send_message = AsyncMock(return_value={"ok": True})
    return client


def _make_registry(registered: bool = True):
    registry = MagicMock()
    registry.modules = {"voice_transcription": MagicMock()} if registered else {}
    return registry


def _make_budget(has_quota: bool = True):
    budget = MagicMock()
    budget.has_quota.return_value = has_quota
    budget.record_api_usage = MagicMock()
    return budget


def _make_recognizer(text: str = "Привет мир"):
    recognizer = MagicMock()
    recognizer.recognize = AsyncMock(return_value=text)
    return recognizer


def _make_settings(speech_provider: str = "yandex"):
    settings = MagicMock()
    settings.speech_provider = speech_provider
    return settings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_voice_fast_path():
    """Non-voice payload passes through unchanged."""
    payload = {"message": {"text": "hello"}}
    session = MagicMock()
    telegram = _make_telegram()
    onboarding = _make_onboarding()

    result = await _maybe_transcribe_voice(
        payload, session=session, telegram_client=telegram, onboarding=onboarding
    )

    assert result is payload
    telegram.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_no_message_fast_path():
    """Payload without 'message' key passes through unchanged."""
    payload = {"callback_query": {}}
    session = MagicMock()
    telegram = _make_telegram()
    onboarding = _make_onboarding()

    result = await _maybe_transcribe_voice(
        payload, session=session, telegram_client=telegram, onboarding=onboarding
    )

    assert result is payload
    telegram.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_feature_not_registered_sends_error():
    """Returns None and sends error when voice_transcription not in registry."""
    payload = _make_voice_payload()
    session = MagicMock()
    telegram = _make_telegram()
    onboarding = _make_onboarding()

    with patch(
        "sreda.services.telegram_bot.get_feature_registry", return_value=_make_registry(registered=False)
    ):
        result = await _maybe_transcribe_voice(
            payload, session=session, telegram_client=telegram, onboarding=onboarding
        )

    assert result is None
    telegram.send_message.assert_awaited_once()
    msg = telegram.send_message.call_args.kwargs["text"]
    assert "/subscriptions" in msg


@pytest.mark.asyncio
async def test_no_quota_sends_error():
    """Returns None and sends error when tenant has no quota."""
    payload = _make_voice_payload()
    session = MagicMock()
    telegram = _make_telegram()
    onboarding = _make_onboarding()
    budget = _make_budget(has_quota=False)

    with (
        patch("sreda.services.telegram_bot.get_feature_registry", return_value=_make_registry()),
        patch("sreda.services.telegram_bot.BudgetService", return_value=budget),
    ):
        result = await _maybe_transcribe_voice(
            payload, session=session, telegram_client=telegram, onboarding=onboarding
        )

    assert result is None
    telegram.send_message.assert_awaited_once()
    msg = telegram.send_message.call_args.kwargs["text"]
    assert "Лимит" in msg


@pytest.mark.asyncio
async def test_duration_too_long_sends_error():
    """Returns None and sends error when voice duration exceeds 30 sec."""
    payload = _make_voice_payload(duration=45)
    session = MagicMock()
    telegram = _make_telegram()
    onboarding = _make_onboarding()
    budget = _make_budget()

    with (
        patch("sreda.services.telegram_bot.get_feature_registry", return_value=_make_registry()),
        patch("sreda.services.telegram_bot.BudgetService", return_value=budget),
    ):
        result = await _maybe_transcribe_voice(
            payload, session=session, telegram_client=telegram, onboarding=onboarding
        )

    assert result is None
    telegram.send_message.assert_awaited_once()
    msg = telegram.send_message.call_args.kwargs["text"]
    assert "30" in msg


@pytest.mark.asyncio
async def test_no_speech_provider_sends_error():
    """Returns None and sends error when no speech recognizer is configured."""
    payload = _make_voice_payload()
    session = MagicMock()
    telegram = _make_telegram()
    onboarding = _make_onboarding()
    budget = _make_budget()

    with (
        patch("sreda.services.telegram_bot.get_feature_registry", return_value=_make_registry()),
        patch("sreda.services.telegram_bot.BudgetService", return_value=budget),
        patch("sreda.services.telegram_bot.get_settings", return_value=_make_settings()),
        patch("sreda.services.telegram_bot.get_speech_recognizer", return_value=None),
    ):
        result = await _maybe_transcribe_voice(
            payload, session=session, telegram_client=telegram, onboarding=onboarding
        )

    assert result is None
    telegram.send_message.assert_awaited_once()
    msg = telegram.send_message.call_args.kwargs["text"]
    assert "не настроен" in msg


@pytest.mark.asyncio
async def test_speech_recognition_error_sends_error():
    """Returns None and sends error when SpeechKit fails."""
    payload = _make_voice_payload()
    session = MagicMock()
    telegram = _make_telegram()
    onboarding = _make_onboarding()
    budget = _make_budget()
    recognizer = MagicMock()
    recognizer.recognize = AsyncMock(side_effect=SpeechRecognitionError("API error"))

    with (
        patch("sreda.services.telegram_bot.get_feature_registry", return_value=_make_registry()),
        patch("sreda.services.telegram_bot.BudgetService", return_value=budget),
        patch("sreda.services.telegram_bot.get_settings", return_value=_make_settings()),
        patch("sreda.services.telegram_bot.get_speech_recognizer", return_value=recognizer),
    ):
        result = await _maybe_transcribe_voice(
            payload, session=session, telegram_client=telegram, onboarding=onboarding
        )

    assert result is None
    telegram.send_message.assert_awaited_once()
    msg = telegram.send_message.call_args.kwargs["text"]
    assert "расшифровать" in msg


@pytest.mark.asyncio
async def test_happy_path_injects_text():
    """Happy path: voice transcribed, text injected into payload, usage recorded."""
    payload = _make_voice_payload(duration=5, file_id="fid1")
    session = MagicMock()
    telegram = _make_telegram(file_path="voice/abc.oga", audio=b"ogg_data")
    onboarding = _make_onboarding(tenant_id="t1", chat_id="c1")
    budget = _make_budget()
    recognizer = _make_recognizer(text="Привет мир")

    with (
        patch("sreda.services.telegram_bot.get_feature_registry", return_value=_make_registry()),
        patch("sreda.services.telegram_bot.BudgetService", return_value=budget),
        patch("sreda.services.telegram_bot.get_settings", return_value=_make_settings()),
        patch("sreda.services.telegram_bot.get_speech_recognizer", return_value=recognizer),
    ):
        result = await _maybe_transcribe_voice(
            payload, session=session, telegram_client=telegram, onboarding=onboarding
        )

    # Voice transcription sends the transcription back and returns None
    # (no further pipeline processing until a chat skill is available).
    assert result is None
    telegram.send_message.assert_awaited_once()
    msg = telegram.send_message.call_args.kwargs["text"]
    assert "Привет мир" in msg

    # Verify file download sequence
    telegram.get_file_info.assert_awaited_once_with("fid1")
    telegram.download_file.assert_awaited_once_with("voice/abc.oga")

    # Verify recognizer called with audio
    recognizer.recognize.assert_awaited_once_with(b"ogg_data")

    # Verify usage recorded
    budget.record_api_usage.assert_called_once_with(
        tenant_id="t1",
        feature_key="voice_transcription",
        provider_key="yandex",
        task_type="speech_recognition",
        credits_consumed=1,
    )
