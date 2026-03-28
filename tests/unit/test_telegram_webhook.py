import base64
from pathlib import Path

from fastapi.testclient import TestClient

from sreda.db.models import AgentRun, AgentThread
from sreda.db.models.billing import TenantSubscription
from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models.core import InboundMessage, Job, OutboxMessage, SecureRecord, Tenant, User, Workspace
from sreda.db.session import get_engine, get_session_factory
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.main import create_app
from sreda.services.billing import BillingService
from sreda.services.secure_storage import load_secure_json

EXISTING_CHAT_ID = "100000003"
NEW_USER_CHAT_ID = "100000004"


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
        session.add(Tenant(id="tenant_1", name="Tenant 1"))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
        session.commit()
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 12345,
        "message": {
            "message_id": 77,
            "chat": {"id": int(EXISTING_CHAT_ID), "type": "private"},
            "text": "мой пароль qwerty и телефон +7 999 123-45-67",
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
    assert inbound.message_text_sanitized == "мой пароль [password] и телефон [phone]"
    assert inbound.contains_sensitive_data is True
    assert inbound.secure_record_id == secure_record.id
    assert secure_record.record_type == "telegram_webhook_raw"
    assert secure_record.record_key == "12345"
    assert "qwerty" not in secure_record.encrypted_json
    assert "+7 999 123-45-67" not in secure_record.encrypted_json
    assert load_secure_json(secure_record) == payload


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
    assert "Привет! Я Среда." in sent_messages[0]["text"]
    assert sent_messages[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Мой статус", "callback_data": "billing:status"}],
            [{"text": "Подписки", "callback_data": "billing:subscriptions"}],
            [{"text": "Подключить ЛК EDS", "callback_data": "onboarding:connect_eds"}],
        ]
    }

    session = get_session_factory()()
    try:
        user = session.query(User).filter(User.telegram_account_id == NEW_USER_CHAT_ID).one()
        tenant = session.get(Tenant, user.tenant_id)
        workspace = session.get(Workspace, f"workspace_tg_{NEW_USER_CHAT_ID}")
    finally:
        session.close()

    assert user.id == f"user_tg_{NEW_USER_CHAT_ID}"
    assert tenant is not None
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

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        session.add(Tenant(id="tenant_1", name="Tenant 1"))
        session.add(Workspace(id="workspace_tg_100000003", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
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

    assert response.status_code == 202
    assert len(sent_messages) == 1
    assert sent_messages[0]["chat_id"] == EXISTING_CHAT_ID
    assert "Мой статус" in sent_messages[0]["text"]
    assert "Сумма к оплате: 0 ₽" in sent_messages[0]["text"]

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

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        session.add(Tenant(id="tenant_1", name="Tenant 1"))
        session.add(Workspace(id="workspace_tg_100000003", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
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
    assert len(answered_callbacks) == 1
    assert len(sent_messages) == 1
    assert "Подписка EDS Monitor подключена." in sent_messages[0]["text"]

    session = get_session_factory()()
    try:
        subscriptions = session.query(TenantSubscription).all()
    finally:
        session.close()

    assert len(subscriptions) == 1


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
        session.add(Tenant(id="tenant_1", name="Tenant 1"))
        session.add(Workspace(id="workspace_tg_100000003", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
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
    assert len(answered_callbacks) == 1
    assert len(sent_messages) == 2
    assert "Дополнительный кабинет EDS подключен." in sent_messages[0]["text"]
    open_button = sent_messages[1]["reply_markup"]["inline_keyboard"][0][0]
    assert open_button["text"] == "Ввести логин и пароль от EDS"


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

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        session.add(Tenant(id="tenant_1", name="Tenant 1"))
        session.add(Workspace(id="workspace_tg_100000003", tenant_id="tenant_1", name="Workspace 1"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=EXISTING_CHAT_ID))
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

    assert len(subscriptions) == 1
