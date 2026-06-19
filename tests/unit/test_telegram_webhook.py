import base64
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sreda.db.models import AgentRun, AgentThread
from sreda.db.models.billing import TenantSubscription
from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models.core import (
    Assistant,
    InboundMessage,
    Job,
    OutboxMessage,
    SecureRecord,
    Tenant,
    TenantFeature,
    User,
    Workspace,
)
from sreda.db.models.eds_monitor import EDSAccount, EDSChangeEvent, EDSClaimState
from sreda.db.session import get_engine, get_session_factory
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.main import create_app
from sreda.services.billing import BillingService
from sreda.services.secure_storage import load_secure_json

EXISTING_CHAT_ID = "100000003"
NEW_USER_CHAT_ID = "100000004"


def _mark_existing_user_welcome_sent(session) -> None:
    """Test fixture helper for existing approved users.

    Existing prod users already have the post-approve welcome flag. These
    webhook tests exercise downstream command/callback handling, not the
    welcome-consumes-first-inbound path.
    """
    from sreda.services.onboarding import mark_welcome_sent

    session.flush()
    mark_welcome_sent(session, "tenant_1", "user_1")


def _allow_housewife_entitlement(monkeypatch) -> None:
    """Keep webhook tests focused on routing/delivery, not billing setup."""
    from sreda.services.entitlement_gate import EntitlementGate, GateResult

    monkeypatch.setattr(
        EntitlementGate,
        "check",
        lambda self, tenant_id: GateResult(
            allowed=True,
            reason="ok",
            plan_key="sreda_free",
            is_grandfathered=False,
        ),
    )


# 2026-04-30: после переезда approved-flow в asyncio.create_task() основная
# обработка идёт в фоне. TestClient уже вернул 202, но background task
# (LLM/voice/outbox/tracе) только начинается. Хелпер ждёт пока условие
# станет true, опрашивая state — типичный wait-for-condition паттерн.
def _wait_for(condition, timeout: float = 5.0, interval: float = 0.05) -> None:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if condition():
                return
        except Exception:  # noqa: BLE001 — condition может бросать пока state ещё не готов
            pass
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for background task")


def test_telegram_webhook_persists_sanitized_and_encrypted_payload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        # approved_at set explicitly: approval gate (2026-04-23) silent-
        # drops tenants with NULL; existing users were auto-approved by
        # migration, so tests mirror that.
        session.add(Tenant(id="tenant_1", name="Tenant 1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        # 2026-04-27: бипасс state-machine онбординга в webhook'е
        # (имя+форма обращения должны быть заполнены, иначе вместо
        # обычного chat-flow приходит вопрос «как тебя зовут?»).
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_test", tenant_id="tenant_1", user_id="user_1",
            display_name="Тестовый Юзер", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 12345,
        "message": {
            "message_id": 77,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "мой пароль qwerty и email me@example.com",
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    assert response.status_code == 202
    body = response.json()
    assert body["ok"] is True
    assert body["request_id"].startswith("in_")

    session = get_session_factory()()
    try:
        inbound = session.query(InboundMessage).one()
        secure_record = session.query(SecureRecord).one()
    finally:
        session.close()

    assert inbound.bot_key == "sreda"
    assert inbound.sender_chat_id == EXISTING_CHAT_ID
    assert inbound.message_text_sanitized == "мой пароль [password] и email [email]"
    assert inbound.contains_sensitive_data is True
    assert inbound.secure_record_id == secure_record.id
    assert secure_record.record_type == "telegram_webhook_raw"
    assert secure_record.record_key == "12345"
    assert "qwerty" not in secure_record.encrypted_json
    assert "me@example.com" not in secure_record.encrypted_json
    assert load_secure_json(secure_record) == payload


@pytest.mark.skip(reason="Phase 2C auto-approve changes new-user flow; rewire test in Phase 2D after tour 4-branch + onboarding_tour_completed flag wiring")
def test_telegram_webhook_creates_new_user_and_sends_welcome_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    sent_messages: list[dict] = []

    async def fake_send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True}

    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.test.local")
    monkeypatch.setattr(TelegramClient, "send_message", fake_send_message)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    client = TestClient(create_app())
    payload = {
        "update_id": 777,
        "message": {
            "message_id": 1,
            "chat": {
                "id": int(NEW_USER_CHAT_ID),
                "type": "private",
                "first_name": "Борис",
                "username": "BorisPechorin",
            },
            "text": "Привет",
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    assert response.status_code == 202
    assert len(sent_messages) == 1
    assert sent_messages[0]["chat_id"] == NEW_USER_CHAT_ID
    # 2026-04-27 (вечер): pending-бот шлёт цепочку из 10 сообщений.
    # Первое — intro с одной кнопкой «🎙️ Голос →» (callback pb:voice),
    # которая ведёт на следующее сообщение про голос.
    text = sent_messages[0]["text"]
    assert "Среда" in text
    assert "тапай" in text.lower() or "кнопк" in text.lower()
    markup = sent_messages[0]["reply_markup"]
    assert markup is not None
    rows = markup["inline_keyboard"]
    assert len(rows) == 1
    btn = rows[0][0]
    assert btn["callback_data"] == "pb:voice"
    assert "Голос" in btn["text"]

    session = get_session_factory()()
    try:
        # 2026-04-27: lookup идёт через services.onboarding.find_user_by_chat_id,
        # которая считает hash от chat_id и матчит User.tg_account_hash.
        # Раньше было `filter(User.telegram_account_id == ...)`, но колонка
        # теперь EncryptedString — равенство по plaintext не работает.
        from sreda.services.onboarding import find_user_by_chat_id

        user = find_user_by_chat_id(session, NEW_USER_CHAT_ID)
        assert user is not None
        tenant = session.get(Tenant, user.tenant_id)
        workspace = session.get(Workspace, f"workspace_tg_{NEW_USER_CHAT_ID}")
    finally:
        session.close()

    assert user.id == f"user_tg_{NEW_USER_CHAT_ID}"
    assert tenant is not None
    assert tenant.approved_at is None, "new tenant must land as pending-approval"
    assert workspace is not None


def test_telegram_webhook_handles_status_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    sent_messages: list[dict] = []

    async def fake_send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}

    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(TelegramClient, "send_message", fake_send_message)
    _allow_housewife_entitlement(monkeypatch)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        # approved_at set explicitly: approval gate (2026-04-23) silent-
        # drops tenants with NULL; existing users were auto-approved by
        # migration, so tests mirror that.
        session.add(Tenant(id="tenant_1", name="Tenant 1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_tg_100000003", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        # 2026-04-27: бипасс state-machine онбординга в webhook'е
        # (имя+форма обращения должны быть заполнены, иначе вместо
        # обычного chat-flow приходит вопрос «как тебя зовут?»).
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_test", tenant_id="tenant_1", user_id="user_1",
            display_name="Тестовый Юзер", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 9001,
        "message": {
            "message_id": 10,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "/status",
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    from sreda.services.ack_messages import all_phrases

    assert response.status_code == 202
    # Two messages now: the fast ack (index 0) + real reply (index 1).
    # Ack is UX sugar — filter it out and assert on the real response.
    _wait_for(lambda: len(sent_messages) == 2)
    assert len(sent_messages) == 2
    assert sent_messages[0]["text"] in all_phrases()
    real_reply = sent_messages[1]
    assert real_reply["chat_id"] == EXISTING_CHAT_ID
    assert "Мой статус" in real_reply["text"]
    # #181: eds_monitor retired. The status payment amount/date are EDS-only
    # figures, so for a tenant with no non-EDS renewal the payment block is
    # suppressed entirely — no "Сумма к оплате: 0 ₽" line for a retired skill.
    assert "Сумма к оплате" not in real_reply["text"]
    assert "Следующий платеж" not in real_reply["text"]

    session = get_session_factory()()
    try:
        jobs = session.query(Job).filter(Job.job_type == "agent.execute_action").all()
        threads = session.query(AgentThread).all()
        runs = session.query(AgentRun).all()
        outbox = session.query(OutboxMessage).all()
    finally:
        session.close()

    assert len(jobs) == 1
    assert len(threads) == 1
    assert len(runs) == 1
    assert len(outbox) == 1
    assert jobs[0].status == "completed"
    assert runs[0].status == "completed"
    assert outbox[0].status == "sent"


def test_telegram_webhook_handles_connect_subscription_callback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    sent_messages: list[dict] = []
    answered_callbacks: list[dict] = []

    async def fake_send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}

    async def fake_answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        answered_callbacks.append({"id": callback_query_id, "text": text})
        return {"ok": True}

    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(TelegramClient, "send_message", fake_send_message)
    monkeypatch.setattr(TelegramClient, "answer_callback_query", fake_answer_callback_query)
    _allow_housewife_entitlement(monkeypatch)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        # approved_at set explicitly: approval gate (2026-04-23) silent-
        # drops tenants with NULL; existing users were auto-approved by
        # migration, so tests mirror that.
        session.add(Tenant(id="tenant_1", name="Tenant 1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_tg_100000003", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        # 2026-04-27: бипасс state-machine онбординга в webhook'е
        # (имя+форма обращения должны быть заполнены, иначе вместо
        # обычного chat-flow приходит вопрос «как тебя зовут?»).
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_test", tenant_id="tenant_1", user_id="user_1",
            display_name="Тестовый Юзер", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 9002,
        "callback_query": {
            "id": "cb_1",
            "data": "billing:connect_plan:eds_monitor_base",
            "message": {
                "message_id": 11,
                "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            },
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    assert response.status_code == 202
    _wait_for(lambda: len(answered_callbacks) == 1 and len(sent_messages) == 1)
    assert len(answered_callbacks) == 1
    assert len(sent_messages) == 1
    # #181: eds_monitor retired. The legacy connect callback still routes
    # (no 404 / silent drop) but the billing mutator is a disabled no-op —
    # the reply is the disabled notice and NO subscription is created.
    assert "Это умение больше не поддерживается." in sent_messages[0]["text"]

    session = get_session_factory()()
    try:
        subscriptions = session.query(TenantSubscription).all()
        jobs = session.query(Job).filter(Job.job_type == "agent.execute_action").all()
        runs = session.query(AgentRun).all()
        outbox = session.query(OutboxMessage).all()
    finally:
        session.close()

    assert len(subscriptions) == 0  # no-op: nothing written
    assert len(jobs) == 1
    assert len(runs) == 1
    assert len(outbox) == 1


@pytest.mark.skip(reason="EDS-monitor scrubbed 2026-05-07; obsolete pending code removal")
def test_telegram_webhook_add_subscription_immediately_starts_eds_binding(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    sent_messages: list[dict] = []
    answered_callbacks: list[dict] = []

    async def fake_send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}

    async def fake_answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        answered_callbacks.append({"id": callback_query_id, "text": text})
        return {"ok": True}

    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")
    monkeypatch.setattr(TelegramClient, "send_message", fake_send_message)
    monkeypatch.setattr(TelegramClient, "answer_callback_query", fake_answer_callback_query)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        # approved_at set explicitly: approval gate (2026-04-23) silent-
        # drops tenants with NULL; existing users were auto-approved by
        # migration, so tests mirror that.
        session.add(Tenant(id="tenant_1", name="Tenant 1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_tg_100000003", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        # 2026-04-27: бипасс state-machine онбординга в webhook'е
        # (имя+форма обращения должны быть заполнены, иначе вместо
        # обычного chat-flow приходит вопрос «как тебя зовут?»).
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_test", tenant_id="tenant_1", user_id="user_1",
            display_name="Тестовый Юзер", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.commit()
        BillingService(session).start_base_subscription("tenant_1")
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 9004,
        "callback_query": {
            "id": "cb_add_subscription",
            "data": "billing:add_eds_account",
            "message": {
                "message_id": 13,
                "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            },
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    assert response.status_code == 202
    _wait_for(lambda: len(answered_callbacks) == 1 and len(sent_messages) == 1)
    assert len(answered_callbacks) == 1
    # Phase: Mini-App-only UX — the reply carries a single "Открыть
    # подписки" web_app button instead of the old per-action "Подключить
    # ЛК EDS" inline. User continues the connect flow inside Mini App.
    assert len(sent_messages) == 1
    assert "Дополнительный кабинет EDS подключен." in sent_messages[0]["text"]
    reply_markup = sent_messages[0]["reply_markup"]
    # reply_markup may be None when base_url is unset in the test env.
    if reply_markup is not None:
        button = reply_markup["inline_keyboard"][0][0]
        assert button["text"] == "Открыть подписки"
        assert "web_app" in button

    session = get_session_factory()()
    try:
        jobs = session.query(Job).filter(Job.job_type == "agent.execute_action").all()
        runs = session.query(AgentRun).all()
        outbox = session.query(OutboxMessage).all()
    finally:
        session.close()

    assert len(jobs) == 1
    assert len(runs) == 1
    assert len(outbox) == 1


def test_telegram_webhook_returns_202_when_telegram_delivery_times_out(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")

    async def failing_send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        raise TelegramDeliveryError("timeout")

    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(TelegramClient, "send_message", failing_send_message)
    _allow_housewife_entitlement(monkeypatch)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        # approved_at set explicitly: approval gate (2026-04-23) silent-
        # drops tenants with NULL; existing users were auto-approved by
        # migration, so tests mirror that.
        session.add(Tenant(id="tenant_1", name="Tenant 1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_tg_100000003", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        # 2026-04-27: бипасс state-machine онбординга в webhook'е
        # (имя+форма обращения должны быть заполнены, иначе вместо
        # обычного chat-flow приходит вопрос «как тебя зовут?»).
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_test", tenant_id="tenant_1", user_id="user_1",
            display_name="Тестовый Юзер", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 9003,
        "callback_query": {
            "id": "cb_timeout",
            "data": "billing:connect_plan:eds_monitor_base",
            "message": {
                "message_id": 12,
                "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            },
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    assert response.status_code == 202

    session = get_session_factory()()
    try:
        subscriptions = session.query(TenantSubscription).all()
    finally:
        session.close()

    # #181: the EDS connect callback is now a no-op (eds_monitor retired), so
    # no subscription is created. The point of this test is the 202-on-
    # delivery-timeout behavior, which is unaffected by the tombstone.
    assert len(subscriptions) == 0


def test_telegram_webhook_rejects_request_without_secret_token_header(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "secret_required.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN", "expected-secret")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    client = TestClient(create_app())
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "hi",
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    assert response.status_code == 401
    # Ensure nothing was persisted — attacker must not be able to trigger side effects
    session = get_session_factory()()
    try:
        assert session.query(InboundMessage).count() == 0
    finally:
        session.close()


def test_telegram_webhook_rejects_request_with_wrong_secret_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "secret_wrong.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN", "expected-secret")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    client = TestClient(create_app())

    response = client.post(
        "/webhooks/telegram/sreda",
        json={"update_id": 2},
        headers={"X-Telegram-Bot-Api-Secret-Token": "attacker-guess"},
    )

    assert response.status_code == 401


def test_telegram_webhook_accepts_request_with_matching_secret_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "secret_ok.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN", "expected-secret")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        # approved_at set explicitly: approval gate (2026-04-23) silent-
        # drops tenants with NULL; existing users were auto-approved by
        # migration, so tests mirror that.
        session.add(Tenant(id="tenant_1", name="Tenant 1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        # 2026-04-27: бипасс state-machine онбординга в webhook'е
        # (имя+форма обращения должны быть заполнены, иначе вместо
        # обычного chat-flow приходит вопрос «как тебя зовут?»).
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_test", tenant_id="tenant_1", user_id="user_1",
            display_name="Тестовый Юзер", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 3,
        "message": {
            "message_id": 1,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "hi",
        },
    }

    response = client.post(
        "/webhooks/telegram/sreda",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
    )

    assert response.status_code == 202


def test_telegram_webhook_handles_claim_lookup_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "claim_webhook.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    sent_messages: list[dict] = []

    async def fake_send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}

    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(TelegramClient, "send_message", fake_send_message)
    _allow_housewife_entitlement(monkeypatch)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        # approved_at set explicitly: approval gate (2026-04-23) silent-
        # drops tenants with NULL; existing users were auto-approved by
        # migration, so tests mirror that.
        session.add(Tenant(id="tenant_1", name="Tenant 1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_tg_100000003", tenant_id="tenant_1", name="Workspace 1"))
        session.flush()
        session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_tg_100000003", name="Sreda"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        # 2026-04-27: бипасс state-machine онбординга в webhook'е
        # (имя+форма обращения должны быть заполнены, иначе вместо
        # обычного chat-flow приходит вопрос «как тебя зовут?»).
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_test", tenant_id="tenant_1", user_id="user_1",
            display_name="Тестовый Юзер", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.add(TenantFeature(id="feature_1", tenant_id="tenant_1", feature_key="eds_monitor", enabled=True))
        session.add(
            EDSAccount(
                id="eds_acc_1",
                tenant_id="tenant_1",
                workspace_id="workspace_tg_100000003",
                assistant_id="assistant_1",
                tenant_eds_account_id=None,
                site_key="mosreg",
                account_key="eds-1",
                label="EDS кабинет 1",
                login_masked="***41",
            )
        )
        session.add(
            EDSClaimState(
                id="state_1",
                eds_account_id="eds_acc_1",
                claim_id="6230173",
                fingerprint_hash="hash_1",
                status="WORK",
                status_name="В работе",
                last_seen_changed="2026-03-28T15:10:00+00:00",
                last_history_order=12,
                last_history_code="HISTORY_SOLVED",
                last_history_date="2026-03-28T15:09:00+00:00",
            )
        )
        session.add(
            EDSChangeEvent(
                id="evt_1",
                eds_account_id="eds_acc_1",
                claim_id="6230173",
                change_type="client_updated",
                has_new_response=True,
                requires_user_action=False,
            )
        )
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 9010,
        "message": {
            "message_id": 21,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "/claim 6230173",
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    from sreda.services.ack_messages import all_phrases

    assert response.status_code == 202
    # Fast ack (index 0) + real claim reply (index 1).
    _wait_for(lambda: len(sent_messages) == 2)
    assert len(sent_messages) == 2
    assert sent_messages[0]["text"] in all_phrases()
    real_reply = sent_messages[1]
    # #181: /claim is an eds_monitor feature — tombstoned. The command still
    # routes and the run completes, but the reply is the disabled notice, not
    # a claim card.
    assert "Это умение отключено." in real_reply["text"]
    assert "Заявка #6230173" not in real_reply["text"]

    session = get_session_factory()()
    try:
        jobs = session.query(Job).filter(Job.job_type == "agent.execute_action").all()
        runs = session.query(AgentRun).all()
        outbox = session.query(OutboxMessage).all()
    finally:
        session.close()

    assert len(jobs) == 1
    assert len(runs) == 1
    assert runs[0].action_type == "claim.lookup"
    assert runs[0].status == "completed"
    assert len(outbox) == 1


def test_telegram_webhook_rejects_unknown_bot_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Phase 9: unknown bot_key must be rejected with 404 before any processing."""
    db_path = tmp_path / "unknown_bot_key.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    # Only 'sreda' bot is configured — no SREDA_HOME_BOT_TOKEN.

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    client = TestClient(create_app())
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "hi",
        },
    }

    response = client.post("/webhooks/telegram/evil_bot", json=payload)

    assert response.status_code == 404
    # Nothing must be persisted — the request must be rejected before ingest.
    session = get_session_factory()()
    try:
        assert session.query(InboundMessage).count() == 0
    finally:
        session.close()


def test_telegram_webhook_accepts_known_bot_key_sreda(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Phase 9: the primary 'sreda' bot_key must pass registry resolution."""
    db_path = tmp_path / "known_bot_key.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        session.add(Tenant(id="tenant_1", name="T1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="W1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_p9", tenant_id="tenant_1", user_id="user_1",
            display_name="Test User", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "hi",
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    # 202 means the known bot_key was accepted and ingest ran.
    assert response.status_code == 202


def test_telegram_webhook_accepts_sreda_home_when_configured(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Phase 9: 'sreda_home' bot_key is accepted when SREDA_HOME_BOT_TOKEN is set."""
    db_path = tmp_path / "home_bot_key.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_HOME_BOT_TOKEN", "home-test-token-xyz")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        session.add(Tenant(id="tenant_1", name="T1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="W1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_home", tenant_id="tenant_1", user_id="user_1",
            display_name="Test User", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 200,
        "message": {
            "message_id": 2,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "hello from home",
        },
    }

    response = client.post("/webhooks/telegram/sreda_home", json=payload)

    # 202 means 'sreda_home' resolved in the registry and ingest ran.
    assert response.status_code == 202


def test_telegram_webhook_rejects_sreda_home_when_not_configured(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Phase 9: 'sreda_home' is rejected with 404 when SREDA_HOME_BOT_TOKEN is absent."""
    db_path = tmp_path / "home_not_configured.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    # Explicitly unset HOME_BOT_TOKEN to ensure it's absent even if set in environment.
    monkeypatch.delenv("SREDA_HOME_BOT_TOKEN", raising=False)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    client = TestClient(create_app())

    response = client.post(
        "/webhooks/telegram/sreda_home",
        json={"update_id": 300, "message": {"message_id": 3,
              "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"}, "text": "x"}},
    )

    assert response.status_code == 404
    session = get_session_factory()()
    try:
        assert session.query(InboundMessage).count() == 0
    finally:
        session.close()


def test_telegram_webhook_rate_limits_excess_requests(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Regression guard for H1: the webhook must refuse traffic above
    the per-IP cap with a ``429``. We set a tiny cap, fire more
    requests than that, and check that the excess lands on 429. The
    rate-limit check runs BEFORE the secret-token verification, so
    the test does not need a real secret to reach the limiter.
    """

    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")

    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_RATE_LIMIT_TELEGRAM_MAX_REQUESTS", "2")
    monkeypatch.setenv("SREDA_RATE_LIMIT_TELEGRAM_WINDOW_SECONDS", "60")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    from sreda.api.deps import reset_rate_limiters

    reset_rate_limiters()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        # approved_at set explicitly: approval gate (2026-04-23) silent-
        # drops tenants with NULL; existing users were auto-approved by
        # migration, so tests mirror that.
        session.add(Tenant(id="tenant_1", name="Tenant 1", approved_at=_dt.now(_tz.utc)))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        # 2026-04-27: бипасс state-machine онбординга в webhook'е
        # (имя+форма обращения должны быть заполнены, иначе вместо
        # обычного chat-flow приходит вопрос «как тебя зовут?»).
        from sreda.db.models.user_profile import TenantUserProfile as _TUP
        session.add(_TUP(
            id="tup_test", tenant_id="tenant_1", user_id="user_1",
            display_name="Тестовый Юзер", address_form="ty",
        ))
        _mark_existing_user_welcome_sent(session)
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 9999,
        "message": {
            "message_id": 1,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "/help",
        },
    }
    try:
        statuses = [
            client.post("/webhooks/telegram/sreda", json=payload).status_code
            for _ in range(4)
        ]
    finally:
        reset_rate_limiters()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()

    # Two allowed (even if they return 202 or 4xx downstream), the
    # remainder must be hard 429s from the limiter.
    assert statuses[0] != 429
    assert statuses[1] != 429
    assert statuses[2] == 429
    assert statuses[3] == 429
