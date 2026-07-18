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

from sreda.services.agent_capabilities import VoiceAccessResult
from sreda.services.budget import QuotaStatus
from sreda.services.entitlement_gate import GateResult
from sreda.services.max_inbound import (
    _extract_max_voice_url,
    _maybe_transcribe_max_voice,
)
from sreda.services.onboarding import MaxOnboardingResult

# #204: voice usage is billed under the carrier agent (manifest
# ``includes_voice=True``), not the legacy "voice_transcription" key.
_CARRIER = "housewife_assistant"


def _patch_voice_access(
    monkeypatch, *, allowed: bool = True, billing_feature_key: str | None = _CARRIER
) -> None:
    """Patch the #204 voice resolver used inside ``_maybe_transcribe_max_voice``
    (imported locally from ``sreda.services.agent_capabilities``)."""
    if allowed:
        result = VoiceAccessResult(True, billing_feature_key, "ok")
    else:
        result = VoiceAccessResult(False, None, "no_voice_carrier")
    monkeypatch.setattr(
        "sreda.services.agent_capabilities.resolve_voice_access",
        lambda s, t: result,
    )


def _make_quota(is_subscribed: bool = False, is_exhausted: bool = False) -> QuotaStatus:
    """Real QuotaStatus so the gate (``is_subscribed AND is_exhausted``) sees
    booleans. Default = housewife sreda_free (not metered → no block)."""
    return QuotaStatus(
        feature_key=_CARRIER,
        is_subscribed=is_subscribed,
        is_exhausted=is_exhausted,
        credits_used=0,
        credits_quota=None,
        period_start=None,
        period_end=None,
    )


def _patch_budget(monkeypatch, quota: QuotaStatus | None = None) -> MagicMock:
    """Patch ``BudgetService`` (imported locally inside the function) with a
    mock whose ``get_quota_status`` returns a real QuotaStatus and whose
    ``record_api_usage`` is observable. Returns the mock instance."""
    instance = MagicMock()
    instance.record_api_usage = MagicMock()
    instance.get_quota_status = MagicMock(return_value=quota or _make_quota())
    monkeypatch.setattr(
        "sreda.services.budget.BudgetService", lambda s: instance
    )
    return instance


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
    _patch_voice_access(monkeypatch, allowed=False)

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

    _patch_voice_access(monkeypatch)
    _patch_budget(monkeypatch)
    _patch_paid_gate(monkeypatch)
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

    _patch_voice_access(monkeypatch)
    _patch_budget(monkeypatch)
    _patch_paid_gate(monkeypatch)
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

    _patch_voice_access(monkeypatch)
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
    _budget = _patch_budget(monkeypatch)

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
    _patch_voice_access(monkeypatch)

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
    _budget = _patch_budget(monkeypatch)
    _patch_paid_gate(monkeypatch)

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

    # #204: usage attributed to the CARRIER, not legacy "voice_transcription".
    # Non-vacuous: reverting feature_key=billing_feature_key → _VOICE_FEATURE_KEY
    # reddens this assertion.
    _budget.record_api_usage.assert_called_once()
    assert _budget.record_api_usage.call_args.kwargs["feature_key"] == _CARRIER


# ---------------------------------------------------------------------------
# #204 Phase 1 — fail-closed + quota gate (MAX)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_d_fail_closed_no_carrier_key_skips_stt_and_usage(monkeypatch):
    """(d) fail-closed: resolver allowed=True но billing_feature_key=None →
    download/STT НЕ зван, usage НЕ записан, отдаётся voice-error."""
    _patch_voice_access(monkeypatch, billing_feature_key=None)
    _budget = _patch_budget(monkeypatch)
    fake_recognizer = MagicMock()
    fake_recognizer.recognize = AsyncMock(return_value="не должно дойти")
    monkeypatch.setattr(
        "sreda.services.speech.factory.get_speech_recognizer",
        lambda settings: fake_recognizer,
    )
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry",
        lambda: MagicMock(modules={"voice_transcription": True}),
    )

    max_client = MagicMock()
    max_client.send_message = AsyncMock()
    max_client.download_audio = AsyncMock(return_value=b"OggS" + b"\x00" * 100)

    result = await _maybe_transcribe_max_voice(
        _voice_payload(),
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is None
    max_client.download_audio.assert_not_awaited()
    fake_recognizer.recognize.assert_not_awaited()
    _budget.record_api_usage.assert_not_called()
    max_client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_max_f_exhausted_metered_carrier_blocks_before_download(monkeypatch):
    """(f) is_subscribed=True И is_exhausted=True → download/STT/usage НЕ
    вызваны, голос блокирован ДО любого reserve/download."""
    _patch_voice_access(monkeypatch)
    _budget = _patch_budget(
        monkeypatch, _make_quota(is_subscribed=True, is_exhausted=True)
    )
    fake_recognizer = MagicMock()
    fake_recognizer.recognize = AsyncMock(return_value="не должно дойти")
    monkeypatch.setattr(
        "sreda.services.speech.factory.get_speech_recognizer",
        lambda settings: fake_recognizer,
    )
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry",
        lambda: MagicMock(modules={"voice_transcription": True}),
    )

    max_client = MagicMock()
    max_client.send_message = AsyncMock()
    max_client.download_audio = AsyncMock(return_value=b"OggS" + b"\x00" * 100)

    result = await _maybe_transcribe_max_voice(
        _voice_payload(),
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is None
    max_client.download_audio.assert_not_awaited()
    fake_recognizer.recognize.assert_not_awaited()
    _budget.record_api_usage.assert_not_called()
    max_client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_max_g_scheduled_carrier_not_subscribed_gate_does_not_block(monkeypatch):
    """(g) scheduled_for_cancel carrier → is_subscribed=False → quota-гейт НЕ
    блокирует (фикс R5-A). Голос работает, usage под carrier."""
    _patch_voice_access(monkeypatch)
    _budget = _patch_budget(
        monkeypatch, _make_quota(is_subscribed=False, is_exhausted=True)
    )
    _patch_paid_gate(monkeypatch)
    fake_recognizer = MagicMock()
    fake_recognizer.recognize = AsyncMock(return_value="расшифровка g")
    monkeypatch.setattr(
        "sreda.services.speech.factory.get_speech_recognizer",
        lambda settings: fake_recognizer,
    )
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry",
        lambda: MagicMock(modules={"voice_transcription": True}),
    )

    max_client = MagicMock()
    max_client.send_message = AsyncMock()
    max_client.download_audio = AsyncMock(return_value=b"OggS" + b"\x00" * 100)

    payload = _voice_payload()
    result = await _maybe_transcribe_max_voice(
        payload,
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is payload
    fake_recognizer.recognize.assert_awaited_once()
    _budget.record_api_usage.assert_called_once()
    assert _budget.record_api_usage.call_args.kwargs["feature_key"] == _CARRIER


# ---------------------------------------------------------------------------
# #204 V6 — exhausted carrier gate sits BEFORE the free-tier llm_turn reserve
# ---------------------------------------------------------------------------
#
# MAX-близнец V6 (см. подробный коммент в test_voice_transcription.py): тест
# (f) доказывает, что download/STT/record_api_usage НЕ зван при исчерпанном
# carrier'е, но НЕ доказывает, что free-tier-резерв ``try_consume('llm_turns')``
# тоже не зван — в (f) ``_is_free`` ложно, ветка free-tier структурно мертва.
# Здесь делаем тенанта РЕАЛЬНО-free (patch EntitlementGate → sreda_free) + spy
# на ``try_consume``: гейт исчерпания (max_inbound.py:608-613) отсекает ход ДО
# резерва (max_inbound.py:649).


def _patch_free_gate(monkeypatch) -> None:
    """EntitlementGate.check → sreda_free → ``_is_free=True`` (резерв достижим).
    Источник: ``max_inbound.py:636`` + ``GateResult`` (entitlement_gate.py:45)."""
    gate = MagicMock()
    gate.check = MagicMock(
        return_value=GateResult(
            allowed=True, reason="ok",
            plan_key="sreda_free", is_grandfathered=False,
        )
    )
    monkeypatch.setattr(
        "sreda.services.entitlement_gate.EntitlementGate", lambda s: gate
    )


def _patch_paid_gate(monkeypatch) -> None:
    """EntitlementGate.check → НЕ-free план → ``_is_free=False`` (paid-path
    без free-tier-резерва, как тесты предполагали до Phase 2C).

    2026-07-18 audit fallout: после фикса окна подписки в гейте
    (``_within_active_window``) голый MagicMock-session падает с TypeError
    на ``row.quantity <= 0`` — патчим публичный контракт ``GateResult``
    (близнец ``_patch_free_gate`` выше), а не форму SQL-row: так менее
    хрупко."""
    gate = MagicMock()
    gate.check = MagicMock(
        return_value=GateResult(
            allowed=True, reason="ok",
            plan_key="housewife_assistant_base", is_grandfathered=False,
        )
    )
    monkeypatch.setattr(
        "sreda.services.entitlement_gate.EntitlementGate", lambda s: gate
    )


def _patch_ledger_spy(monkeypatch) -> MagicMock:
    """UsageLedgerService с spy на ``try_consume`` (sync). Возвращает ledger-mock."""
    ledger = MagicMock()
    ledger.try_consume = MagicMock(return_value=True)
    ledger.refund = MagicMock()
    monkeypatch.setattr(
        "sreda.services.usage_ledger.UsageLedgerService", lambda engine: ledger
    )
    return ledger


@pytest.mark.asyncio
async def test_max_v6_exhausted_carrier_does_not_reserve_llm_turn(monkeypatch):
    """V6 (MAX): РЕАЛЬНО-free тенант + исчерпанный метрированный carrier →
    гейт исчерпания отсекает ход ДО free-tier-резерва:
    ``UsageLedgerService.try_consume`` НЕ зван."""
    _patch_voice_access(monkeypatch)
    _budget = _patch_budget(
        monkeypatch, _make_quota(is_subscribed=True, is_exhausted=True)
    )
    _patch_free_gate(monkeypatch)
    ledger = _patch_ledger_spy(monkeypatch)

    fake_recognizer = MagicMock()
    fake_recognizer.recognize = AsyncMock(return_value="не должно дойти")
    monkeypatch.setattr(
        "sreda.services.speech.factory.get_speech_recognizer",
        lambda settings: fake_recognizer,
    )
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry",
        lambda: MagicMock(modules={"voice_transcription": True}),
    )

    max_client = MagicMock()
    max_client.send_message = AsyncMock()
    max_client.download_audio = AsyncMock(return_value=b"OggS" + b"\x00" * 100)

    result = await _maybe_transcribe_max_voice(
        _voice_payload(),
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is None
    ledger.try_consume.assert_not_called()
    max_client.download_audio.assert_not_awaited()
    fake_recognizer.recognize.assert_not_awaited()
    _budget.record_api_usage.assert_not_called()
    max_client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_max_v6_non_exhausted_free_carrier_does_reserve_llm_turn(monkeypatch):
    """V6 не-вакуумность (MAX): тот же РЕАЛЬНО-free тенант, но carrier НЕ исчерпан
    → гейт НЕ срабатывает → ``try_consume('llm_turns', 1)`` ДОСТИГНУТ. Доказывает,
    что резерв реально достижим, и ``assert_not_called`` в exhausted-кейсе —
    следствие гейта, а не мёртвой ветки."""
    _patch_voice_access(monkeypatch)
    _budget = _patch_budget(
        monkeypatch, _make_quota(is_subscribed=False, is_exhausted=False)
    )
    _patch_free_gate(monkeypatch)
    ledger = _patch_ledger_spy(monkeypatch)

    fake_recognizer = MagicMock()
    fake_recognizer.recognize = AsyncMock(return_value="расшифровка v6b")
    monkeypatch.setattr(
        "sreda.services.speech.factory.get_speech_recognizer",
        lambda settings: fake_recognizer,
    )
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry",
        lambda: MagicMock(modules={"voice_transcription": True}),
    )
    # ffprobe duration нужен для voice-резерва ПОСЛЕ download.
    # audit 2026-07-18: max_inbound зовёт probe_audio_async → ffprobe_duration
    # получает kwarg timeout_sec — lambda обязана его принимать.
    monkeypatch.setattr(
        "sreda.services.audio_probe.ffprobe_duration", lambda b, timeout_sec=10.0: 3.0
    )

    max_client = MagicMock()
    max_client.send_message = AsyncMock()
    max_client.download_audio = AsyncMock(return_value=b"OggS" + b"\x00" * 100)

    payload = _voice_payload()
    result = await _maybe_transcribe_max_voice(
        payload,
        session=MagicMock(),
        max_client=max_client,
        onboarding=_onboarding(),
    )

    assert result is payload
    assert ledger.try_consume.call_count >= 1
    first_call = ledger.try_consume.call_args_list[0]
    assert first_call.args[0] == "t1"        # tenant_id (из _onboarding)
    assert first_call.args[1] == "llm_turns"
    assert first_call.args[2] == 1
    max_client.send_message.assert_not_awaited()
