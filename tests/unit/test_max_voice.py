"""Phase 10.4 — MAX voice transcription tests.

Covers:
- ``_extract_max_voice_url`` — payload audio extraction edge cases
- ``_maybe_transcribe_max_voice`` — passthrough non-voice, oversize
  (~30s/1MB, raised by streaming downloader as MaxDeliveryError 413),
  generic download error, successful inject, transcript persistence
- ``MaxClient.download_audio`` — host allowlist, http rejection, URL
  redaction in logs

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


def test_extract_voice_url_caption_takes_precedence():
    """Codex R1 MAJOR #8: audio + non-empty body.text (caption) →
    treat as text turn, skip STT (caption приоритет)."""
    p = _voice_payload()
    p["message"]["body"]["text"] = "Послушай мой пожжалуй"  # caption
    assert _extract_max_voice_url(p) is None


# ---------------------------------------------------------------------------
# MaxClient.download_audio — security guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_audio_rejects_untrusted_host():
    """Codex R1 CRITICAL #1: только хосты в _ALLOWED_AUDIO_HOSTS.

    Webhook payload может быть подделан → URL вне allowlist'а — SSRF.
    """
    from sreda.integrations.max import MaxClient

    client = MaxClient(token="test-token")
    with pytest.raises(ValueError, match="not in allowlist"):
        await client.download_audio("https://evil.example.com/audio.ogg")


@pytest.mark.asyncio
async def test_download_audio_rejects_http_scheme():
    """https-only — http был бы downgrade attack."""
    from sreda.integrations.max import MaxClient

    client = MaxClient(token="test-token")
    with pytest.raises(ValueError, match="not in allowlist"):
        await client.download_audio("http://a.oneme.ru/audio")


def test_redact_audio_url_strips_signature_query():
    """URL с signed token → query string redacted в loggable form."""
    from sreda.integrations.max import MaxClient

    raw = "https://a.oneme.ru/audio?cid=X&signatureToken=SECRET&expires=123"
    redacted = MaxClient._redact_audio_url(raw)
    assert "SECRET" not in redacted
    assert "signatureToken" not in redacted
    assert redacted.startswith("https://a.oneme.ru/audio?")


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
    функция возвращает None → caller прерывает обработку.

    Codex R1 MINOR #11 fix: production imports `get_feature_registry`
    inside the function from `sreda.features.app_registry`; patch на
    том же import path, не на re-export'е в max_inbound module.
    """
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry",
        lambda: MagicMock(modules={"voice_transcription": True}),
    )
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
async def test_transcribe_oversize_via_413_error_sends_too_long(monkeypatch):
    """audio > _MAX_AUDIO_DOWNLOAD_BYTES → download_audio streaming
    early-aborts с MaxDeliveryError(status_code=413).

    Codex R4 fix: раньше тест мокировал download_audio чтобы вернуть
    >limit байт (impossible в реале — download_audio сам обрезает),
    и в helper'е срабатывала defence-in-depth byte-cap. Реальный prod
    flow — exception 413 от MaxClient. Тестируем реалистичный path.
    """
    from sreda.integrations.max.client import MaxDeliveryError

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
    max_client.download_audio = AsyncMock(side_effect=MaxDeliveryError(
        "audio download exceeds cap 1000000 bytes",
        method="audio_download_oversize", status_code=413,
    ))

    result = await _maybe_transcribe_max_voice(
        _voice_payload(),
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is None
    sent_text = max_client.send_message.await_args.kwargs["text"]
    assert "слишком длинн" in sent_text.lower()
    assert "30 сек" in sent_text.lower() or "~30" in sent_text.lower()


@pytest.mark.asyncio
async def test_transcribe_generic_download_error_sends_retry_message(monkeypatch):
    """Non-413 MaxDeliveryError (network/4xx-non-413/timeout) → generic
    «не удалось получить» error message, не «слишком длинное»."""
    from sreda.integrations.max.client import MaxDeliveryError

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
    max_client.download_audio = AsyncMock(side_effect=MaxDeliveryError(
        "audio download timeout",
        method="audio_download", status_code=None,
    ))

    result = await _maybe_transcribe_max_voice(
        _voice_payload(),
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is None
    sent_text = max_client.send_message.await_args.kwargs["text"]
    assert "не удалось получить" in sent_text.lower()
    # NOT the «слишком длинное» message — должно быть отдельная ветка
    assert "слишком" not in sent_text.lower()


@pytest.mark.asyncio
async def test_transcribe_persists_sanitized_to_inbound(monkeypatch):
    """Codex R2 fix lock-in: после STT transcript пишется в
    ``inbound.message_text_sanitized`` через privacy_guard.

    Регрессия R1 fix #6 — был ``sanitized_result.matches`` (атрибута
    нет, AttributeError → silent rollback). Этот тест проверяет что
    persistence работает с настоящим InboundMessage row.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sreda.db.base import Base
    from sreda.db.models.core import InboundMessage, Tenant

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T1"))
    sess.add(
        InboundMessage(
            id="in_persist", tenant_id="t1",
            bot_key="sreda_max", channel_type="max",
            external_update_id="mid.persist",
            status="accepted",
            processing_status="processing_started",
            contains_sensitive_data=False,
        )
    )
    sess.commit()

    monkeypatch.setattr(
        "sreda.services.agent_capabilities.has_voice_access",
        lambda s, t: True,
    )
    fake_recognizer = MagicMock()
    fake_recognizer.recognize = AsyncMock(return_value="осмысленный transcribed текст")
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
    max_client.download_audio = AsyncMock(return_value=b"OggS\x00\x02" + b"\x00" * 200)

    payload = _voice_payload()
    result = await _maybe_transcribe_max_voice(
        payload,
        session=sess,
        max_client=max_client,
        onboarding=_onboarding(),
        inbound_message_id="in_persist",
    )

    assert result is payload
    assert payload["message"]["body"]["text"] == "осмысленный transcribed текст"

    # Verify persistence to DB row
    sess.expire_all()
    row = sess.get(InboundMessage, "in_persist")
    assert row.message_text_sanitized == "осмысленный transcribed текст"

    sess.close()


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
