import asyncio
import base64

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.connect import ConnectSession, TenantEDSAccount
from sreda.db.models.core import Assistant, Job, SecureRecord, Tenant, TenantFeature, User, Workspace
from sreda.db.models.eds_monitor import EDSAccount
from sreda.db.session import get_engine, get_session_factory
from sreda.services.billing import BillingService
from sreda.services.eds_account_verification import (
    EDSAccountVerificationService,
    VerificationError,
    VerificationResult,
)
from sreda.services.eds_connect import EDSConnectService
from sreda.services.encryption import get_encryption_service
from sreda.services.secure_storage import load_secure_json


class FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True}


class SuccessAdapter:
    async def verify_account(
        self,
        *,
        account_key: str,
        login: str,
        password: str,
    ) -> VerificationResult:
        assert account_key.startswith("teds_")
        assert login == "5047136341"
        assert password == "super-secret"
        return VerificationResult(login_masked="5047***341")


class AuthFailAdapter:
    async def verify_account(
        self,
        *,
        account_key: str,
        login: str,
        password: str,
    ) -> VerificationResult:
        raise VerificationError(
            "verification_auth_failed",
            "Не удалось подключить кабинет. Проверь логин и пароль.",
            retryable=False,
        )


class TemporaryFailAdapter:
    async def verify_account(
        self,
        *,
        account_key: str,
        login: str,
        password: str,
    ) -> VerificationResult:
        raise VerificationError(
            "verification_temporary_failed",
            "Временная ошибка подключения. Попробуй еще раз позже.",
            retryable=True,
        )


def test_successful_verification_activates_account_and_sends_message(monkeypatch, tmp_path) -> None:
    session = _build_session(monkeypatch, tmp_path)
    telegram_client = FakeTelegramClient()
    try:
        job_id = _seed_submitted_connect(session)
        service = EDSAccountVerificationService(
            session,
            telegram_client=telegram_client,
            adapter=SuccessAdapter(),
        )

        result = asyncio.run(service.process_job(job_id))

        connect_session = session.query(ConnectSession).one()
        tenant_account = session.query(TenantEDSAccount).one()
        job = session.query(Job).one()
        credential_record = (
            session.query(SecureRecord)
            .filter(SecureRecord.record_type == "eds_account_credentials")
            .one()
        )
        runtime_account = session.query(EDSAccount).one()
    finally:
        session.close()

    payload = load_secure_json(credential_record)
    assert result == "completed"
    assert job.status == "completed"
    assert connect_session.status == "verified"
    assert tenant_account.status == "active"
    assert tenant_account.secure_record_id == credential_record.id
    assert runtime_account.tenant_eds_account_id == tenant_account.id
    assert runtime_account.account_key == tenant_account.id
    assert runtime_account.label == "EDS кабинет 1"
    assert runtime_account.login == "5047136341"
    assert payload["login"] == "5047136341"
    assert payload["password"] == "super-secret"
    assert len(telegram_client.messages) == 1
    assert "Кабинет EDS подключен." in telegram_client.messages[0]["text"]


def test_auth_failure_marks_account_failed_and_sends_retry(monkeypatch, tmp_path) -> None:
    session = _build_session(monkeypatch, tmp_path)
    telegram_client = FakeTelegramClient()
    try:
        job_id = _seed_submitted_connect(session)
        service = EDSAccountVerificationService(
            session,
            telegram_client=telegram_client,
            adapter=AuthFailAdapter(),
        )

        result = asyncio.run(service.process_job(job_id))

        connect_session = session.query(ConnectSession).one()
        tenant_account = session.query(TenantEDSAccount).one()
        job = session.query(Job).one()
    finally:
        session.close()

    assert result == "failed"
    assert job.status == "failed"
    assert connect_session.status == "failed"
    assert connect_session.error_code == "verification_auth_failed"
    assert tenant_account.status == "auth_failed"
    assert len(telegram_client.messages) == 1
    assert "Проверь логин и пароль" in telegram_client.messages[0]["text"]
    assert (
        telegram_client.messages[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        == "eds:retry_connect:primary"
    )


def test_temporary_failure_keeps_job_pending_for_retry(monkeypatch, tmp_path) -> None:
    session = _build_session(monkeypatch, tmp_path)
    telegram_client = FakeTelegramClient()
    try:
        job_id = _seed_submitted_connect(session)
        service = EDSAccountVerificationService(
            session,
            telegram_client=telegram_client,
            adapter=TemporaryFailAdapter(),
        )

        result = asyncio.run(service.process_job(job_id))

        connect_session = session.query(ConnectSession).one()
        tenant_account = session.query(TenantEDSAccount).one()
        job = session.query(Job).one()
    finally:
        session.close()

    assert result == "retry_scheduled"
    assert job.status == "pending"
    assert connect_session.status == "submitted"
    assert tenant_account.status == "pending_verification"
    assert "\"attempts\": 1" in job.payload_json
    assert telegram_client.messages == []


def _build_session(monkeypatch, tmp_path):
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    get_encryption_service.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    engine = create_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_bundle(session)
    return session


def _seed_submitted_connect(session) -> str:
    BillingService(session).start_base_subscription("tenant_1")
    link = EDSConnectService(session, _build_test_settings()).create_connect_link(
        tenant_id="tenant_1",
        workspace_id="workspace_1",
        user_id="user_1",
        slot_type="primary",
    )
    submit_result = EDSConnectService(session, _build_test_settings()).submit_form(
        link.raw_token,
        login="5047136341",
        password="super-secret",
    )
    return submit_result.job_id


def _seed_bundle(session) -> None:
    session.add(Tenant(id="tenant_1", name="Tenant 1"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000011"))
    session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Среда"))
    session.add(
        TenantFeature(
            id="tenant_1:core_assistant",
            tenant_id="tenant_1",
            feature_key="core_assistant",
            enabled=True,
        )
    )
    session.commit()


def _build_test_settings():
    from sreda.config.settings import Settings

    return Settings(
        encryption_key=base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii"),
        connect_public_base_url="https://connect.example.test",
    )
