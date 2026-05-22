"""Phase 10.1 — wire MAX bot to conversation graph.

Covers:
- ``dispatch_max_action`` — text → ActionEnvelope, including free-form
  → ``conversation.chat`` (closes R1 review CRITICAL #1: verifies the
  shared ``_resolve_command_action`` fallback semantics that the plan
  relies on)
- ``to_outbox_channel`` + ``ActionEnvelope.outbox_channel`` — surface ↔
  bare channel mapping
- ``_process_approved_max_turn`` — wiring smoke test
  (dispatch + ActionRuntimeService, tenant lock, inbound status lifecycle)

Tests use real SQLAlchemy session (in-memory sqlite) для consistency
с ``test_max_inbound.py`` / ``test_channel_linking.py`` patterns.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.db.models.user_profile import TenantUserProfile
from sreda.runtime.dispatcher import (
    ActionEnvelope,
    dispatch_max_action,
    to_outbox_channel,
)
from sreda.services.housewife_onboarding import record_pb_tour_progress
from sreda.services.onboarding import MaxOnboardingResult
from sreda.services.tenant_lock import reset_for_tests


@pytest.fixture(autouse=True)
def _reset_tenant_locks():
    """Cross-test isolation: tenant_lock dict — module-level state.

    Без сброса между тестами лок задержанный в asyncio.Lock из предыдущей
    test-loop'ы будет привязан к мёртвому event loop'у → следующий тест
    залипает на `async with`.
    """
    yield
    reset_for_tests()


@pytest.fixture()
def db():
    """Provide both an engine factory and a probe session.

    Wiring tests need ``get_session_factory`` to produce **new** session
    per call (т.к. ``_process_approved_max_turn`` open'ит свой и close'ит
    в finally). Возвращаем struct с engine + maker + probe-session для
    setup'а данных + verify post-call state из свежей сессии.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    setup_sess = SessionLocal()
    setup_sess.add(Tenant(id="t1", name="T1"))
    setup_sess.add(Workspace(id="w1", tenant_id="t1", name="Home"))
    setup_sess.add(User(id="u1", tenant_id="t1", max_account_id="40921122"))
    setup_sess.commit()
    setup_sess.close()

    class _DB:
        sessionmaker = SessionLocal

        def session(self):
            return SessionLocal()

    yield _DB()


@pytest.fixture()
def session(db):
    """Single-session fixture для дешёвых non-wiring тестов."""
    sess = db.session()
    try:
        yield sess
    finally:
        sess.close()


def _make_onboarding(
    *,
    tenant_id: str = "t1",
    user_id: str = "u1",
    workspace_id: str = "w1",
    assistant_id: str | None = "a1",
    max_account_id: str | None = "40921122",
    max_chat_id: str | None = "320955459",
    is_new_user: bool = False,
) -> MaxOnboardingResult:
    """Build a fully-populated MaxOnboardingResult for dispatch tests."""
    return MaxOnboardingResult(
        tenant_id=tenant_id,
        user_id=user_id,
        workspace_id=workspace_id,
        assistant_id=assistant_id,
        max_account_id=max_account_id,
        max_chat_id=max_chat_id,
        is_new_user=is_new_user,
    )


def _payload_message_created(text: str) -> dict:
    """Build a minimal MAX `message_created` payload (probe Phase 0 shape)."""
    return {
        "update_type": "message_created",
        "timestamp": 1777907183208,
        "message": {
            "recipient": {"chat_id": 320955459, "chat_type": "dialog"},
            "body": {
                "mid": "mid.test_dispatch",
                "seq": 1,
                "text": text,
            },
            "sender": {
                "user_id": 40921122,
                "first_name": "Борис",
                "name": "Борис",
                "is_bot": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# to_outbox_channel — surface → bare mapping
# ---------------------------------------------------------------------------


def test_to_outbox_channel_telegram_dm():
    assert to_outbox_channel("telegram_dm") == "telegram"


def test_to_outbox_channel_max_dm():
    assert to_outbox_channel("max_dm") == "max"


def test_to_outbox_channel_idempotent_bare_telegram():
    assert to_outbox_channel("telegram") == "telegram"


def test_to_outbox_channel_idempotent_bare_max():
    assert to_outbox_channel("max") == "max"


def test_to_outbox_channel_unknown_passthrough_with_warning(caplog):
    """Unknown channel passes through unchanged + WARNING log fires.

    R1 review #7 — explicit warning instead of silent passthrough.
    """
    import logging
    caplog.set_level(logging.WARNING, logger="sreda.runtime.dispatcher")
    result = to_outbox_channel("whatsapp_dm")
    assert result == "whatsapp_dm"
    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("whatsapp_dm" in m for m in warning_messages), \
        f"expected warning mentioning 'whatsapp_dm', got {warning_messages!r}"


def test_action_envelope_outbox_channel_property():
    """ActionEnvelope.outbox_channel should derive from channel_type."""
    env = ActionEnvelope(
        action_type="conversation.chat",
        tenant_id="t1", workspace_id="w1", assistant_id=None, user_id="u1",
        channel_type="max_dm",
        external_chat_id="320955459",
        bot_key="sreda_max",
        inbound_message_id=None,
        source_type="max_message",
        source_value="привет",
        params={"text": "привет"},
    )
    assert env.outbox_channel == "max"

    env_tg = ActionEnvelope(
        action_type="conversation.chat",
        tenant_id="t1", workspace_id="w1", assistant_id=None, user_id="u1",
        channel_type="telegram_dm",
        external_chat_id="320955459",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_message",
        source_value="привет",
        params={"text": "привет"},
    )
    assert env_tg.outbox_channel == "telegram"


# ---------------------------------------------------------------------------
# dispatch_max_action — message → envelope
# ---------------------------------------------------------------------------


def test_dispatch_max_free_form_text_routes_to_conversation_chat():
    """R2 review residual #1 + R1 CRITICAL #1 closer.

    Free-form Russian text должен мапиться на ``conversation.chat`` —
    это default LLM path. Без этого MAX-бот игнорировал бы любое
    сообщение которое не command. Тест верифицирует что
    ``_resolve_command_action`` (shared с TG) включает fallback и наша
    обвязка его пропускает.
    """
    onboarding = _make_onboarding()
    payload = _payload_message_created("привет")

    envelope = dispatch_max_action(
        payload=payload,
        bot_key="sreda_max",
        onboarding=onboarding,
        inbound_message_id="in_test_1",
    )

    assert envelope is not None, "free-form text должен вернуть envelope, не None"
    assert envelope.action_type == "conversation.chat"
    assert envelope.params == {"text": "привет"}
    assert envelope.channel_type == "max_dm"
    assert envelope.outbox_channel == "max"
    assert envelope.external_chat_id == "320955459"
    assert envelope.bot_key == "sreda_max"
    assert envelope.source_type == "max_message"
    assert envelope.source_value == "привет"
    assert envelope.inbound_message_id == "in_test_1"


def test_dispatch_max_command_routes_to_typed_action():
    """`/help` через shared resolver → help.show envelope."""
    onboarding = _make_onboarding()
    payload = _payload_message_created("/help")

    envelope = dispatch_max_action(
        payload=payload,
        bot_key="sreda_max",
        onboarding=onboarding,
        inbound_message_id="in_test_2",
    )

    assert envelope is not None
    assert envelope.action_type == "help.show"


def test_dispatch_max_subscriptions_command_works():
    """Russian command ``подписки`` тоже должен резолвиться (shared path)."""
    onboarding = _make_onboarding()
    payload = _payload_message_created("подписки")

    envelope = dispatch_max_action(
        payload=payload, bot_key="sreda_max",
        onboarding=onboarding, inbound_message_id=None,
    )

    assert envelope is not None
    assert envelope.action_type == "subscriptions.show"


def test_dispatch_max_empty_body_returns_none():
    """Whitespace-only text → None (silent ignore по плану)."""
    onboarding = _make_onboarding()
    payload = _payload_message_created("   ")

    envelope = dispatch_max_action(
        payload=payload, bot_key="sreda_max",
        onboarding=onboarding, inbound_message_id=None,
    )

    assert envelope is None


def test_dispatch_max_bot_started_no_text_returns_none():
    """`bot_started` без text body → None (welcome flow — separate task)."""
    onboarding = _make_onboarding()
    payload = {
        "update_type": "bot_started",
        "chat_id": 320955459,
        "user": {"user_id": 40921122, "name": "Борис"},
    }

    envelope = dispatch_max_action(
        payload=payload, bot_key="sreda_max",
        onboarding=onboarding, inbound_message_id=None,
    )

    assert envelope is None


def test_dispatch_max_unsupported_update_type_returns_none():
    """Non-message events (e.g. bot_added) → None."""
    onboarding = _make_onboarding()
    payload = {"update_type": "bot_added", "chat_id": 320955459}

    envelope = dispatch_max_action(
        payload=payload, bot_key="sreda_max",
        onboarding=onboarding, inbound_message_id=None,
    )

    assert envelope is None


def test_dispatch_max_missing_chat_id_returns_none():
    """Onboarding без max_chat_id → нельзя ответить → None."""
    onboarding = _make_onboarding(max_chat_id=None)
    payload = _payload_message_created("привет")

    envelope = dispatch_max_action(
        payload=payload, bot_key="sreda_max",
        onboarding=onboarding, inbound_message_id=None,
    )

    assert envelope is None


def test_dispatch_max_callback_routes_through_resolve_callback_action():
    """message_callback с known callback_data → ActionEnvelope.

    Проверяем что ``payload.callback.payload`` (MAX-специфичный путь —
    TG кладёт в ``callback_query.data``) extracted и резолвится через
    тот же ``_resolve_callback_action`` что и TG.
    """
    from sreda.services.billing import SUBSCRIPTIONS_CALLBACK

    onboarding = _make_onboarding()
    payload = {
        "update_type": "message_callback",
        "timestamp": 1777994922729,
        "callback": {
            "callback_id": "cb_xyz",
            "user": {"user_id": 40921122, "name": "Борис"},
            "payload": SUBSCRIPTIONS_CALLBACK,  # known TG-style callback_data
        },
        "message": {
            "recipient": {"chat_id": 320955459},
            "body": {"mid": "mid.x"},
            "sender": {"user_id": 290524257, "is_bot": True},
        },
    }

    envelope = dispatch_max_action(
        payload=payload, bot_key="sreda_max",
        onboarding=onboarding, inbound_message_id="in_cb_1",
    )

    assert envelope is not None
    assert envelope.action_type == "subscriptions.show"
    assert envelope.channel_type == "max_dm"
    assert envelope.source_type == "max_callback"
    assert envelope.source_value == SUBSCRIPTIONS_CALLBACK


def test_dispatch_max_callback_unknown_data_returns_none():
    """Unknown callback_data (e.g. ``test_done``) — None, caller рутит
    через inline-handler в max_inbound._handle_max_callback."""
    onboarding = _make_onboarding()
    payload = {
        "update_type": "message_callback",
        "callback": {
            "callback_id": "cb_x",
            "user": {"user_id": 40921122, "name": "Борис"},
            "payload": "test_done",  # not in _ACTION_BY_CALLBACK
        },
        "message": {
            "recipient": {"chat_id": 320955459},
            "body": {"mid": "m"},
            "sender": {"user_id": 290524257, "is_bot": True},
        },
    }

    envelope = dispatch_max_action(
        payload=payload, bot_key="sreda_max",
        onboarding=onboarding, inbound_message_id=None,
    )

    assert envelope is None


def test_dispatch_max_callback_missing_payload_returns_none():
    """Malformed callback (no payload) — graceful None, не raise."""
    onboarding = _make_onboarding()
    payload = {
        "update_type": "message_callback",
        "callback": {"callback_id": "x", "user": {"user_id": 40921122}},
        "message": {"recipient": {"chat_id": 320955459}},
    }
    envelope = dispatch_max_action(
        payload=payload, bot_key="sreda_max",
        onboarding=onboarding, inbound_message_id=None,
    )
    assert envelope is None


def test_dispatch_max_voice_or_image_no_text_returns_none():
    """Probe Phase 0: MAX message без body.text (e.g. голос/картинка)
    extractор возвращает None → dispatch отдаёт None."""
    onboarding = _make_onboarding()
    payload = {
        "update_type": "message_created",
        "message": {
            "recipient": {"chat_id": 320955459},
            "body": {"mid": "m1"},  # без text
        },
    }

    envelope = dispatch_max_action(
        payload=payload, bot_key="sreda_max",
        onboarding=onboarding, inbound_message_id=None,
    )

    assert envelope is None


# ---------------------------------------------------------------------------
# _process_approved_max_turn — wiring smoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_approved_max_turn_dispatches_and_runs_runtime(db, monkeypatch):
    """End-to-end wiring smoke: dispatch_max_action → ActionRuntimeService.

    Mock ``ActionRuntimeService.enqueue_action`` + ``process_job`` чтобы
    не запускать реальный LangGraph. Verify что:
    - dispatch_max_action вызвана
    - enqueue_action получила envelope с channel_type='max_dm'
    - process_job вызвана с queued.job_id
    - inbound row помечена 'processed'
    """
    from sreda.db.models.core import InboundMessage
    from sreda.services import max_inbound

    # Setup inbound row через probe session (закроем сразу — функция
    # создаст свою через session_factory).
    setup = db.session()
    setup.add(
        InboundMessage(
            id="in_smoke", tenant_id="t1",
            bot_key="sreda_max", channel_type="max",
            external_update_id="mid.smoke", status="received",
            processing_status="ingested",
        )
    )
    setup.commit()
    setup.close()

    onboarding = _make_onboarding()

    # Mock ActionRuntimeService entirely
    fake_runtime = MagicMock()
    fake_runtime.enqueue_action = MagicMock(return_value=MagicMock(job_id="job_1"))
    fake_runtime.process_job = AsyncMock(return_value="completed")
    runtime_factory = MagicMock(return_value=fake_runtime)
    monkeypatch.setattr(
        "sreda.runtime.executor.ActionRuntimeService",
        runtime_factory,
    )
    # session_factory returns the same sessionmaker — функция получит
    # свежую session per call.
    monkeypatch.setattr(
        max_inbound, "get_session_factory",
        lambda: db.sessionmaker,
    )

    payload = _payload_message_created("привет")

    await max_inbound._process_approved_max_turn(
        bot_key="sreda_max",
        payload=payload,
        onboarding=onboarding,
        inbound_message_id="in_smoke",
    )

    # Verify wiring
    runtime_factory.assert_called_once()
    fake_runtime.enqueue_action.assert_called_once()
    enqueued_envelope = fake_runtime.enqueue_action.call_args.args[0]
    assert enqueued_envelope.action_type == "conversation.chat"
    assert enqueued_envelope.channel_type == "max_dm"
    assert enqueued_envelope.outbox_channel == "max"
    fake_runtime.process_job.assert_awaited_once_with("job_1")

    # Verify inbound status lifecycle через свежую session
    verify = db.session()
    try:
        row = verify.get(InboundMessage, "in_smoke")
        assert row.processing_status == "processed"
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_process_approved_max_turn_no_dispatchable_action_marks_ignored(
    db, monkeypatch,
):
    """If dispatch_max_action returns None (e.g. bot_added) → status='ignored'.

    Runtime НЕ дёргается.
    """
    from sreda.db.models.core import InboundMessage
    from sreda.services import max_inbound

    setup = db.session()
    setup.add(
        InboundMessage(
            id="in_ignore", tenant_id="t1",
            bot_key="sreda_max", channel_type="max",
            external_update_id="ignore.1", status="received",
            processing_status="ingested",
        )
    )
    setup.commit()
    setup.close()

    runtime_factory = MagicMock()
    monkeypatch.setattr(
        "sreda.runtime.executor.ActionRuntimeService",
        runtime_factory,
    )
    monkeypatch.setattr(
        max_inbound, "get_session_factory",
        lambda: db.sessionmaker,
    )

    onboarding = _make_onboarding()
    payload = {"update_type": "bot_added"}

    await max_inbound._process_approved_max_turn(
        bot_key="sreda_max",
        payload=payload,
        onboarding=onboarding,
        inbound_message_id="in_ignore",
    )

    runtime_factory.assert_not_called()
    verify = db.session()
    try:
        row = verify.get(InboundMessage, "in_ignore")
        assert row.processing_status == "ignored"
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_process_approved_max_turn_saves_post_tour_name_without_runtime(
    db, monkeypatch,
):
    """After pb:done, MAX text is captured as display_name, not sent to LLM."""
    from sreda.db.models.core import InboundMessage
    from sreda.services import max_inbound

    setup = db.session()
    setup.add(
        InboundMessage(
            id="in_name_max", tenant_id="t1",
            bot_key="sreda_max", channel_type="max",
            external_update_id="mid.name", status="received",
            processing_status="ingested",
        )
    )
    record_pb_tour_progress(
        setup,
        tenant_id="t1",
        user_id="u1",
        branch="done",
    )
    setup.commit()
    setup.close()

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs) -> dict:
            raise AssertionError("name confirmation must go through outbox")
            return {"success": True}

    class _Settings:
        max_bot_token = "max-token"

    runtime_factory = MagicMock()
    monkeypatch.setattr(
        "sreda.runtime.executor.ActionRuntimeService",
        runtime_factory,
    )
    monkeypatch.setattr(
        max_inbound, "get_session_factory",
        lambda: db.sessionmaker,
    )
    monkeypatch.setattr(max_inbound, "get_settings", lambda: _Settings())
    monkeypatch.setattr(max_inbound, "MaxClient", _Client)
    monkeypatch.setattr(
        "sreda.services.housewife_onboarding.extract_pb_tour_display_name_with_llm",
        lambda text: "Борис Аркадьевич",
    )

    await max_inbound._process_approved_max_turn(
        bot_key="sreda_max",
        payload=_payload_message_created("Меня зовут Борис Аркадьевич"),
        onboarding=_make_onboarding(),
        inbound_message_id="in_name_max",
    )

    runtime_factory.assert_not_called()

    verify = db.session()
    try:
        profile = verify.query(TenantUserProfile).filter_by(
            tenant_id="t1", user_id="u1",
        ).one()
        row = verify.get(InboundMessage, "in_name_max")
        outbox = verify.query(OutboxMessage).one()
        payload = json.loads(outbox.payload_json)
        assert profile.display_name == "Борис Аркадьевич"
        assert row.processing_status == "processed"
        assert outbox.feature_key == "onboarding_name_confirm"
        assert outbox.channel_type == "max"
        assert payload["chat_id"] == "320955459"
        assert payload["attachments"] == []
        assert "Борис Аркадьевич" in payload["text"]
        assert "что будем делать первым" in payload["text"]
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_process_approved_max_turn_voice_can_capture_post_tour_name(
    db, monkeypatch,
):
    from sreda.db.models.core import InboundMessage
    from sreda.services import max_inbound

    setup = db.session()
    setup.add(
        InboundMessage(
            id="in_voice_name_max", tenant_id="t1",
            bot_key="sreda_max", channel_type="max",
            external_update_id="mid.voice", status="received",
            processing_status="ingested",
        )
    )
    record_pb_tour_progress(
        setup,
        tenant_id="t1",
        user_id="u1",
        branch="done",
    )
    setup.commit()
    setup.close()

    class _Settings:
        max_bot_token = "max-token"
        ack_edit_max_enabled = False

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

    async def fake_transcribe(payload, **kwargs):
        payload["message"]["body"]["text"] = "меня зовут Катя"
        return payload

    runtime_factory = MagicMock()

    monkeypatch.setattr(
        "sreda.runtime.executor.ActionRuntimeService",
        runtime_factory,
    )
    monkeypatch.setattr(
        max_inbound, "get_session_factory",
        lambda: db.sessionmaker,
    )
    monkeypatch.setattr(max_inbound, "get_settings", lambda: _Settings())
    monkeypatch.setattr(max_inbound, "MaxClient", _Client)
    monkeypatch.setattr(
        max_inbound,
        "_maybe_transcribe_max_voice",
        fake_transcribe,
    )
    monkeypatch.setattr(
        "sreda.services.housewife_onboarding.extract_pb_tour_display_name_with_llm",
        lambda text: "Катя",
    )

    await max_inbound._process_approved_max_turn(
        bot_key="sreda_max",
        payload={"update_type": "message_created", "message": {"body": {}}},
        onboarding=_make_onboarding(),
        inbound_message_id="in_voice_name_max",
    )

    runtime_factory.assert_not_called()

    verify = db.session()
    try:
        profile = verify.query(TenantUserProfile).filter_by(
            tenant_id="t1", user_id="u1",
        ).one()
        assert profile.display_name == "Катя"
        outbox = verify.query(OutboxMessage).one()
        payload = json.loads(outbox.payload_json)
        assert outbox.feature_key == "onboarding_name_confirm"
        assert "Катя" in payload["text"]
        row = verify.get(InboundMessage, "in_voice_name_max")
        assert row.processing_status == "processed"
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_handle_max_callback_unknown_payload_acks_and_returns_false(
    db, monkeypatch,
):
    """Generic callback (e.g. ``test_done``) → answer_callback вызвана,
    handler возвращает False (caller продолжает через dispatcher).
    """
    from sreda.services import max_inbound

    onboarding = _make_onboarding()
    payload = {
        "update_type": "message_callback",
        "callback": {
            "callback_id": "cb_test_42",
            "user": {"user_id": 40921122, "name": "Борис"},
            "payload": "test_done",
        },
        "message": {"recipient": {"chat_id": 320955459}},
    }

    fake_max_client = MagicMock()
    fake_max_client.answer_callback = AsyncMock(return_value={"ok": True})

    sess = db.session()
    try:
        handled = await max_inbound._handle_max_callback(
            session=sess,
            max_client=fake_max_client,
            payload=payload,
            onboarding=onboarding,
        )
    finally:
        sess.close()

    assert handled is False  # caller routes via dispatcher
    fake_max_client.answer_callback.assert_awaited_once()
    call_args = fake_max_client.answer_callback.call_args
    assert call_args.args[0] == "cb_test_42"


@pytest.mark.asyncio
async def test_handle_max_callback_reminder_done_completes_and_acks(
    db, monkeypatch,
):
    """``rem_done:<id>`` → FamilyReminder acknowledged + toast'ed."""
    from datetime import datetime, timezone

    from sreda.db.models.housewife import FamilyReminder
    from sreda.services import max_inbound

    setup = db.session()
    setup.add(
        FamilyReminder(
            id="rem_test_1",
            tenant_id="t1",
            user_id=None,  # tenant-wide for test simplicity
            title="купить молоко",
            trigger_at=datetime.now(timezone.utc),
            next_trigger_at=datetime.now(timezone.utc),
            status="pending",
        )
    )
    setup.commit()
    setup.close()

    onboarding = _make_onboarding()
    payload = {
        "update_type": "message_callback",
        "callback": {
            "callback_id": "cb_rem_42",
            "user": {"user_id": 40921122, "name": "Борис"},
            "payload": "rem_done:rem_test_1",
        },
        "message": {"recipient": {"chat_id": 320955459}},
    }

    fake_max_client = MagicMock()
    fake_max_client.answer_callback = AsyncMock(return_value={"ok": True})

    sess = db.session()
    try:
        handled = await max_inbound._handle_max_callback(
            session=sess,
            max_client=fake_max_client,
            payload=payload,
            onboarding=onboarding,
        )
    finally:
        sess.close()

    assert handled is True  # inline path consumed turn

    # answer_callback вызвана с message replacement (per MAX UX —
    # toast notification невидим, заменяем body целиком)
    fake_max_client.answer_callback.assert_awaited_once()
    call = fake_max_client.answer_callback.call_args
    assert call.args[0] == "cb_rem_42"
    msg = call.kwargs.get("message", {})
    assert "✅" in msg.get("text", "")

    # Reminder помечен fired + acknowledged_at set (one-shot path)
    verify = db.session()
    try:
        rem = verify.get(FamilyReminder, "rem_test_1")
        assert rem.acknowledged_at is not None
        assert rem.status == "fired"
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_handle_max_callback_cross_tenant_refuses_mutation(db, monkeypatch):
    """Callback от tenant t1, reminder в tenant t2 → refuse, не mutate."""
    from datetime import datetime, timezone

    from sreda.db.models.housewife import FamilyReminder
    from sreda.services import max_inbound

    setup = db.session()
    setup.add(Tenant(id="t2", name="T2"))
    setup.commit()
    setup.add(
        FamilyReminder(
            id="rem_other",
            tenant_id="t2",  # OTHER tenant
            user_id=None,  # tenant-wide; nullable
            title="чужое",
            trigger_at=datetime.now(timezone.utc),
            next_trigger_at=datetime.now(timezone.utc),
            status="pending",
        )
    )
    setup.commit()
    setup.close()

    onboarding = _make_onboarding(tenant_id="t1")
    payload = {
        "update_type": "message_callback",
        "callback": {
            "callback_id": "cb_x",
            "user": {"user_id": 40921122},
            "payload": "rem_done:rem_other",
        },
        "message": {"recipient": {"chat_id": 320955459}},
    }

    fake_max_client = MagicMock()
    fake_max_client.answer_callback = AsyncMock(return_value={"ok": True})

    sess = db.session()
    try:
        handled = await max_inbound._handle_max_callback(
            session=sess,
            max_client=fake_max_client,
            payload=payload,
            onboarding=onboarding,
        )
    finally:
        sess.close()

    assert handled is True  # inline path responds (refuse)
    # Verify reminder UNCHANGED (status still "pending", not acked)
    verify = db.session()
    try:
        rem = verify.get(FamilyReminder, "rem_other")
        assert rem.status == "pending"  # NOT modified
        assert rem.acknowledged_at is None
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_process_approved_max_turn_uses_shared_tenant_lock(db, monkeypatch):
    """Same tenant_lock instance shared с TG path (R1 #4 closer).

    Acquire lock from telegram path's accessor, then start MAX turn —
    оно ждёт пока TG отпустит. Без shared module это не работало бы
    (отдельные dict'ы → отдельные locks).
    """
    import asyncio

    from sreda.db.models.core import InboundMessage
    from sreda.services import max_inbound
    from sreda.services.tenant_lock import get_tenant_lock as max_lock_getter
    from sreda.services.telegram_inbound import _get_tenant_lock as tg_lock_getter

    # Same lock object for same tenant_id
    assert max_lock_getter("t1") is tg_lock_getter("t1"), \
        "Cross-channel lock must be the same Lock object"

    setup = db.session()
    setup.add(
        InboundMessage(
            id="in_lock", tenant_id="t1",
            bot_key="sreda_max", channel_type="max",
            external_update_id="lock.1", status="received",
            processing_status="ingested",
        )
    )
    setup.commit()
    setup.close()

    fake_runtime = MagicMock()
    fake_runtime.enqueue_action = MagicMock(return_value=MagicMock(job_id="job_l"))
    fake_runtime.process_job = AsyncMock()
    monkeypatch.setattr(
        "sreda.runtime.executor.ActionRuntimeService",
        MagicMock(return_value=fake_runtime),
    )
    monkeypatch.setattr(
        max_inbound, "get_session_factory",
        lambda: db.sessionmaker,
    )

    # Pre-acquire lock from TG-side accessor
    lock = tg_lock_getter("t1")
    await lock.acquire()
    try:
        async def turn_then_unlock():
            await asyncio.sleep(0.05)
            lock.release()

        unlock_task = asyncio.create_task(turn_then_unlock())
        # process should block on lock acquisition until unlock_task fires
        await asyncio.wait_for(
            max_inbound._process_approved_max_turn(
                bot_key="sreda_max",
                payload=_payload_message_created("hi"),
                onboarding=_make_onboarding(),
                inbound_message_id="in_lock",
            ),
            timeout=2.0,
        )
        await unlock_task
    finally:
        if lock.locked():
            lock.release()

    fake_runtime.process_job.assert_awaited_once()
