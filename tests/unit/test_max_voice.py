"""Phase 10.4 — MAX voice transcription tests.

Covers:
- ``_extract_max_voice_url`` — payload audio extraction edge cases
- ``_maybe_transcribe_max_voice`` — passthrough non-voice, byte-cap,
  successful inject, no STT recognizer

Probe Phase 10.4 (Boris 2026-05-05): MAX delivers voice as
``message.body.attachments[].type=='audio'`` with ``payload.url`` —
signed direct download URL (no auth header, signature in query).
``body.text=""`` for voice-only messages.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sreda.services.max_inbound import (
    _extract_max_voice_url,
    _maybe_transcribe_max_voice,
)
from sreda.services.onboarding import MaxOnboardingResult


def _voice_payload(*, url: str = "https://a.oneme.ru/audio?cid=test") -> dict:
    """Real-shape MAX voice payload (probe-confirmed)."""
    return {
        "update_type": "message_created",
        "message": {
            "recipient": {"chat_id": 320955459, "chat_type": "dialog"},
            "body": {
                "mid": "mid.test_voice",
                "text": "",
                "attachments": [
                    {
                        "type": "audio",
                        "payload": {
                            "url": url,
                            "token": "sig_token",
                            "id": 12345,
                        },
                    }
                ],
            },
            "sender": {"user_id": 40921122, "name": "Борис"},
        },
    }


def _text_payload() -> dict:
    return {
        "update_type": "message_created",
        "message": {
            "body": {"mid": "mid.txt", "text": "Привет"},
            "sender": {"user_id": 40921122},
        },
    }


def _onboarding() -> MaxOnboardingResult:
    return MaxOnboardingResult(
        tenant_id="t1", user_id="u1", workspace_id="w1",
        assistant_id="a1",
        max_account_id="40921122", max_chat_id="320955459",
        is_new_user=False,
    )


# ---------------------------------------------------------------------------
# _extract_max_voice_url
# ---------------------------------------------------------------------------


def test_extract_voice_url_happy():
    p = _voice_payload(url="https://a.oneme.ru/audio?x=1")
    assert _extract_max_voice_url(p) == "https://a.oneme.ru/audio?x=1"


def test_extract_voice_url_text_only_returns_none():
    assert _extract_max_voice_url(_text_payload()) is None


def test_extract_voice_url_no_message_returns_none():
    assert _extract_max_voice_url({"update_type": "bot_started"}) is None


def test_extract_voice_url_attachment_wrong_type_returns_none():
    p = _voice_payload()
    p["message"]["body"]["attachments"][0]["type"] = "image"
    assert _extract_max_voice_url(p) is None


def test_extract_voice_url_missing_url_returns_none():
    p = _voice_payload()
    del p["message"]["body"]["attachments"][0]["payload"]["url"]
    assert _extract_max_voice_url(p) is None


def test_extract_voice_url_finds_audio_among_multiple():
    """Если в attachments несколько типов — берём первый audio."""
    p = _voice_payload()
    p["message"]["body"]["attachments"] = [
        {"type": "image", "payload": {"url": "https://img"}},
        {"type": "audio", "payload": {"url": "https://aud"}},
    ]
    assert _extract_max_voice_url(p) == "https://aud"


# ---------------------------------------------------------------------------
# _maybe_transcribe_max_voice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_passthrough_non_voice():
    """Text-only payload → returns same payload unchanged, no client call."""
    max_client = MagicMock()
    max_client.send_message = AsyncMock()
    max_client.download_audio = AsyncMock()

    payload = _text_payload()
    result = await _maybe_transcribe_max_voice(
        payload,
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is payload  # identity, no copy
    max_client.send_message.assert_not_awaited()
    max_client.download_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_transcribe_no_voice_access_sends_error_and_returns_none(
    monkeypatch,
):
    """Если у tenant'а нет voice access → юзер видит русский error,
    функция возвращает None → caller прерывает обработку."""
    from sreda.services import max_inbound as mi

    monkeypatch.setattr(
        mi, "get_feature_registry",
        lambda: MagicMock(modules={"voice_transcription": True}),
        raising=False,
    )
    # has_voice_access → False
    monkeypatch.setattr(
        "sreda.services.agent_capabilities.has_voice_access",
        lambda s, t: False,
    )

    max_client = MagicMock()
    max_client.send_message = AsyncMock()
    max_client.download_audio = AsyncMock()

    result = await _maybe_transcribe_max_voice(
        _voice_payload(),
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is None
    max_client.send_message.assert_awaited_once()
    sent_text = max_client.send_message.await_args.kwargs["text"]
    assert "подписк" in sent_text.lower()  # Russian
    max_client.download_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_transcribe_byte_cap_rejects_long_audio(monkeypatch):
    """audio > 2MB → user видит русский error «слишком длинное»."""
    from sreda.services import max_inbound as mi

    monkeypatch.setattr(
        mi, "_VOICE_MAX_BYTES", 100,  # tiny cap для теста
    )
    monkeypatch.setattr(
        "sreda.services.agent_capabilities.has_voice_access",
        lambda s, t: True,
    )
    monkeypatch.setattr(
        "sreda.services.speech.factory.get_speech_recognizer",
        lambda settings: MagicMock(),
    )
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry",
        lambda: MagicMock(modules={"voice_transcription": True}),
    )

    max_client = MagicMock()
    max_client.send_message = AsyncMock()
    max_client.download_audio = AsyncMock(return_value=b"x" * 500)  # too big

    result = await _maybe_transcribe_max_voice(
        _voice_payload(),
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is None
    sent_text = max_client.send_message.await_args.kwargs["text"]
    assert "слишком длинн" in sent_text.lower()


@pytest.mark.asyncio
async def test_transcribe_success_injects_text_into_body(monkeypatch):
    """Happy path: download → STT → inject в body.text → caller видит
    text turn."""
    monkeypatch.setattr(
        "sreda.services.agent_capabilities.has_voice_access",
        lambda s, t: True,
    )

    fake_recognizer = MagicMock()
    fake_recognizer.recognize = AsyncMock(return_value="расшифрованный текст")
    monkeypatch.setattr(
        "sreda.services.speech.factory.get_speech_recognizer",
        lambda settings: fake_recognizer,
    )
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry",
        lambda: MagicMock(modules={"voice_transcription": True}),
    )
    monkeypatch.setattr(
        "sreda.services.budget.BudgetService",
        lambda s: MagicMock(record_api_usage=MagicMock()),
    )

    max_client = MagicMock()
    max_client.send_message = AsyncMock()
    max_client.download_audio = AsyncMock(return_value=b"OggS\x00..." + b"\x00" * 100)

    payload = _voice_payload()
    result = await _maybe_transcribe_max_voice(
        payload,
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is payload
    assert payload["message"]["body"]["text"] == "расшифрованный текст"
    fake_recognizer.recognize.assert_awaited_once()
    max_client.send_message.assert_not_awaited()  # no error reply
