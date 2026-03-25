import base64
from pathlib import Path

from fastapi.testclient import TestClient

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models.connect import ConnectSession, TenantEDSAccount
from sreda.db.models.core import Assistant, Job, SecureRecord, Tenant, TenantFeature, User, Workspace
from sreda.db.session import get_engine, get_session_factory
from sreda.integrations.telegram.client import TelegramClient
from sreda.main import create_app
from sreda.services.billing import BillingService
from sreda.services.eds_connect import EDSConnectService
from sreda.services.secure_storage import load_secure_json


CHAT_ID = "100000010"


def test_connect_callback_creates_one_time_link(
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
        _seed_bundle(session)
        BillingService(session).start_base_subscription("tenant_1")
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 9101,
        "callback_query": {
            "id": "cb_connect",
            "data": "onboarding:connect_eds",
            "message": {
                "message_id": 11,
                "chat": {"id": int(CHAT_ID), "type": "private"},
            },
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    assert response.status_code == 202
    assert len(answered_callbacks) == 1
    assert len(sent_messages) == 1
    assert "Ссылка действует 15 минут" in sent_messages[0]["text"]
    open_button = sent_messages[0]["reply_markup"]["inline_keyboard"][0][0]
    assert open_button["text"] == "Открыть Mini App"
    assert open_button["web_app"]["url"].startswith("https://connect.example.test/connect/eds/")

    session = get_session_factory()()
    try:
        connect_session = session.query(ConnectSession).one()
    finally:
        session.close()

    assert connect_session.status == "created"


def test_open_connect_form_returns_html_for_valid_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        _seed_bundle(session)
        BillingService(session).start_base_subscription("tenant_1")
        link = EDSConnectService(session, get_settings()).create_connect_link(
            tenant_id="tenant_1",
            workspace_id="workspace_1",
            user_id="user_1",
            slot_type="primary",
        )
        token = link.raw_token
    finally:
        session.close()

    client = TestClient(create_app())
    response = client.get(f"/connect/eds/{token}")

    assert response.status_code == 200
    assert "Подключение кабинета EDS" in response.text
    assert "Логин" in response.text
    assert "Пароль" in response.text


def test_submit_connect_form_stores_secure_payload_and_queues_job(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        _seed_bundle(session)
        BillingService(session).start_base_subscription("tenant_1")
        link = EDSConnectService(session, get_settings()).create_connect_link(
            tenant_id="tenant_1",
            workspace_id="workspace_1",
            user_id="user_1",
            slot_type="primary",
        )
        token = link.raw_token
    finally:
        session.close()

    client = TestClient(create_app())
    response = client.post(
        f"/connect/eds/{token}",
        data={"login": "5047136341", "password": "super-secret"},
    )

    assert response.status_code == 200
    assert "Данные получены" in response.text

    session = get_session_factory()()
    try:
        connect_session = session.query(ConnectSession).one()
        tenant_account = session.query(TenantEDSAccount).one()
        secure_record = session.query(SecureRecord).filter(SecureRecord.record_type == "eds_connect_payload").one()
        job = session.query(Job).one()
    finally:
        session.close()

    payload = load_secure_json(secure_record)
    assert connect_session.status == "submitted"
    assert connect_session.secure_record_id == secure_record.id
    assert connect_session.tenant_eds_account_id == tenant_account.id
    assert tenant_account.status == "pending_verification"
    assert tenant_account.login_masked == "5047***341"
    assert payload["login"] == "5047136341"
    assert payload["password"] == "super-secret"
    assert job.job_type == "eds.verify_account_connect"


def _seed_bundle(session) -> None:
    session.add(Tenant(id="tenant_1", name="Tenant 1"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=CHAT_ID))
    session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Среда"))
    session.add(TenantFeature(id="tenant_1:core_assistant", tenant_id="tenant_1", feature_key="core_assistant", enabled=True))
    session.commit()
