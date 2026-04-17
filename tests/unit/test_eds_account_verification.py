import asyncio
import base64
import json
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.connect import ConnectSession, TenantEDSAccount
from sreda.db.models.core import Assistant, Job, SecureRecord, Tenant, TenantFeature, User, Workspace
from sreda.db.models.eds_monitor import EDSAccount
from sreda.db.session import get_engine, get_session_factory
from sreda.integrations.telegram.client import TelegramDeliveryError
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


class FailingTelegramClient:
    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        raise TelegramDeliveryError("timeout")


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
        return VerificationResult(login_masked="***41")


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


class HangingAdapter:
    """Adapter whose ``verify_account`` blocks forever — simulates a
    hung EDS endpoint. Without ``asyncio.wait_for`` guarding the call
    site, the enclosing job would remain in ``running`` state
    indefinitely.
    """

    def __init__(self) -> None:
        self.hit_count = 0

    async def verify_account(
        self,
        *,
        account_key: str,
        login: str,
        password: str,
    ) -> VerificationResult:
        self.hit_count += 1
        await asyncio.sleep(3600)
        return VerificationResult(login_masked="hang")


class LeakingPasswordAdapter:
    """Adapter whose ``VerificationError`` message leaks the raw
    password. This is the exact shape of a careless upstream integration
    that embeds the credential into its error text — the sanitization
    layer must scrub it before it hits the database or a Telegram
    message.
    """

    async def verify_account(
        self,
        *,
        account_key: str,
        login: str,
        password: str,
    ) -> VerificationResult:
        raise VerificationError(
            "verification_auth_failed",
            f"Ошибка авторизации (login={login}, password={password}).",
            retryable=False,
        )


def test_failure_persists_sanitized_error_message_not_raw_password(monkeypatch, tmp_path) -> None:
    """Regression guard for H8: a ``VerificationError.message`` that
    accidentally contains credentials must be redacted before being
    written to ``connect_session.error_message_sanitized`` /
    ``tenant_account.last_error_message_sanitized`` / the Telegram
    failure notification. The field name implies sanitization — until
    this fix, the content was the raw upstream string.
    """

    session = _build_session(monkeypatch, tmp_path)
    telegram_client = FakeTelegramClient()
    try:
        job_id = _seed_submitted_connect(session)
        service = EDSAccountVerificationService(
            session,
            telegram_client=telegram_client,
            adapter=LeakingPasswordAdapter(),
        )

        asyncio.run(service.process_job(job_id))

        connect_session = session.query(ConnectSession).one()
        tenant_accounts = session.query(TenantEDSAccount).all()
    finally:
        session.close()

    # Non-retryable auth failure удаляет tenant_eds_account (slot
    # освобождается). Санитизация проверяется на единственном
    # сохранившемся канале — connect_session + Telegram-сообщение.
    assert tenant_accounts == []
    assert "super-secret" not in (connect_session.error_message_sanitized or "")
    assert "5047136341" not in (connect_session.error_message_sanitized or "")
    assert "[password]" in (connect_session.error_message_sanitized or "")
    assert "[login]" in (connect_session.error_message_sanitized or "")

    # Chat notifications for EDS verification — no-op since 2026-04
    # (Mini App is the canonical surface). Sanitization is checked on
    # connect_session.error_message_sanitized above — that's the only
    # persistent channel now.
    assert telegram_client.messages == []


def test_verification_process_job_fails_fast_when_adapter_hangs(monkeypatch, tmp_path) -> None:
    """Regression guard for H4 in the EDS verification service: a
    hanging adapter must be cancelled within ``job_max_runtime_seconds``
    and the job moved into a terminal ``failed`` state so retries can
    proceed.
    """

    monkeypatch.setenv("SREDA_JOB_MAX_RUNTIME_SECONDS", "0.25")
    session = _build_session(monkeypatch, tmp_path)
    adapter = HangingAdapter()
    try:
        job_id = _seed_submitted_connect(session)
        service = EDSAccountVerificationService(
            session,
            telegram_client=FakeTelegramClient(),
            adapter=adapter,
        )

        result = asyncio.run(service.process_job(job_id))

        connect_session = session.query(ConnectSession).one()
        tenant_accounts = session.query(TenantEDSAccount).all()
        job = session.query(Job).one()
    finally:
        session.close()

    assert adapter.hit_count == 1
    assert result == "failed"
    assert job.status == "failed"
    assert connect_session.status == "failed"
    assert connect_session.error_code == "verification_timeout"
    # verification_timeout — non-retryable (см. NON_RETRYABLE_CLEANUP_CODES),
    # tenant_eds_account удаляется, slot освобождается.
    assert tenant_accounts == []


def test_duplicate_login_does_not_create_second_active_account(monkeypatch, tmp_path) -> None:
    session = _build_session(monkeypatch, tmp_path)
    telegram_client = FakeTelegramClient()
    try:
        first_job_id = _seed_submitted_connect(session)
        service = EDSAccountVerificationService(
            session,
            telegram_client=telegram_client,
            adapter=SuccessAdapter(),
        )
        first_result = asyncio.run(service.process_job(first_job_id))
        BillingService(session).add_extra_eds_account("tenant_1")
        second_job_id = _seed_submitted_connect(
            session,
            slot_type="extra",
            login="5047136341",
            password="super-secret",
        )

        second_result = asyncio.run(service.process_job(second_job_id))

        tenant_accounts = session.query(TenantEDSAccount).order_by(TenantEDSAccount.created_at.asc()).all()
        runtime_accounts = session.query(EDSAccount).order_by(EDSAccount.id.asc()).all()
        connect_sessions = session.query(ConnectSession).order_by(ConnectSession.created_at.asc()).all()
    finally:
        session.close()

    assert first_result == "completed"
    assert second_result == "failed"
    # duplicate_login non-retryable → extra tenant_eds_account удаляется,
    # slot освобождается. Остаётся только primary (active).
    assert len(tenant_accounts) == 1
    assert tenant_accounts[0].status == "active"
    assert tenant_accounts[0].account_role == "primary"
    assert len(runtime_accounts) == 1
    assert runtime_accounts[0].tenant_eds_account_id == tenant_accounts[0].id
    assert connect_sessions[-1].status == "failed"
    assert connect_sessions[-1].error_code == "verification_duplicate_login"
    assert "уже подключен" in connect_sessions[-1].error_message_sanitized
    # Chat notifications (both success and duplicate-failure) are no-ops
    # since 2026-04. State transitions above are the source of truth;
    # the user sees the duplicate-login error on the connect form itself
    # and in the Mini App.
    assert telegram_client.messages == []


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
    assert runtime_account.login_masked == "***41"
    assert payload["login"] == "5047136341"
    assert payload["password"] == "super-secret"
    # Chat notification on success — no-op since 2026-04. Success is
    # visible on the connect form (/connect/eds/<token> success page)
    # and in the Mini App. Keeping the test scoped to state transitions.
    assert telegram_client.messages == []


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
        tenant_accounts = session.query(TenantEDSAccount).all()
        job = session.query(Job).one()
    finally:
        session.close()

    assert result == "failed"
    assert job.status == "failed"
    assert connect_session.status == "failed"
    assert connect_session.error_code == "verification_auth_failed"
    # Non-retryable failure → tenant_eds_account удаляется, slot
    # освобождается. Retry-кнопка в Telegram создаст fresh row.
    assert tenant_accounts == []
    assert connect_session.tenant_eds_account_id is None
    # Chat notification with retry button — no-op since 2026-04. The
    # user sees the failure on the connect form and in the Mini App
    # (account status = auth_failed). Retry is initiated by reopening
    # /subscriptions → Mini App → Подключить ЛК EDS which spins up a
    # fresh connect_session.
    assert telegram_client.messages == []


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


def test_successful_verification_completes_even_if_telegram_delivery_fails(monkeypatch, tmp_path) -> None:
    session = _build_session(monkeypatch, tmp_path)
    try:
        job_id = _seed_submitted_connect(session)
        service = EDSAccountVerificationService(
            session,
            telegram_client=FailingTelegramClient(),
            adapter=SuccessAdapter(),
        )

        result = asyncio.run(service.process_job(job_id))

        connect_session = session.query(ConnectSession).one()
        tenant_account = session.query(TenantEDSAccount).one()
        job = session.query(Job).one()
    finally:
        session.close()

    assert result == "completed"
    assert job.status == "completed"
    assert connect_session.status == "verified"
    assert tenant_account.status == "active"


def test_process_job_is_race_safe_under_concurrent_workers(monkeypatch, tmp_path) -> None:
    # Seed DB with a pending eds.verify_account_connect job, then close the
    # seeding session so both worker threads start from fresh state.
    db_path = tmp_path / "test.db"
    setup_session = _build_session(monkeypatch, tmp_path)
    try:
        job_id = _seed_submitted_connect(setup_session)
    finally:
        setup_session.close()

    # Widen the window between "load job" and "claim job" so both workers
    # race on the same pending row. Without the CAS fix both threads pass
    # the pending-status check and both invoke the adapter, leaving duplicate
    # secure records / runtime accounts behind.
    gate = threading.Barrier(2, timeout=5)
    original_load_payload = EDSAccountVerificationService._load_payload_record

    def gated_load_payload(self, connect_session):
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return original_load_payload(self, connect_session)

    monkeypatch.setattr(
        EDSAccountVerificationService,
        "_load_payload_record",
        gated_load_payload,
    )

    results: list[str] = []
    adapter_calls: list[str] = []
    errors: list[BaseException] = []

    class CountingAdapter:
        async def verify_account(
            self,
            *,
            account_key: str,
            login: str,
            password: str,
        ) -> VerificationResult:
            adapter_calls.append(account_key)
            return VerificationResult(login_masked="***41")

    def worker() -> None:
        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        session = sessionmaker(bind=engine)()
        try:
            service = EDSAccountVerificationService(
                session,
                telegram_client=FakeTelegramClient(),
                adapter=CountingAdapter(),
            )
            result = asyncio.run(service.process_job(job_id))
            results.append(result)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()
            engine.dispose()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)

    assert not errors, f"worker raised: {errors}"
    assert len(results) == 2

    # Verify DB state is consistent: exactly one success, one skip; only
    # one runtime account / credential record was created.
    verify_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    verify_session = sessionmaker(bind=verify_engine)()
    try:
        runtime_accounts = verify_session.query(EDSAccount).all()
        credential_records = (
            verify_session.query(SecureRecord)
            .filter(SecureRecord.record_type == "eds_account_credentials")
            .all()
        )
        tenant_accounts = verify_session.query(TenantEDSAccount).all()
        job = verify_session.query(Job).one()
    finally:
        verify_session.close()
        verify_engine.dispose()

    assert len(adapter_calls) == 1, f"adapter was called {len(adapter_calls)} times"
    assert results.count("completed") == 1
    # Loser should report that the job was already claimed.
    assert results.count("claimed_by_other") == 1
    assert len(runtime_accounts) == 1
    assert len(credential_records) == 1
    assert len(tenant_accounts) == 1
    assert job.status == "completed"


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


def _seed_submitted_connect(
    session,
    *,
    slot_type: str = "primary",
    login: str = "5047136341",
    password: str = "super-secret",
) -> str:
    BillingService(session).start_base_subscription("tenant_1")
    link = EDSConnectService(session, _build_test_settings()).create_connect_link(
        tenant_id="tenant_1",
        workspace_id="workspace_1",
        user_id="user_1",
        slot_type=slot_type,
    )
    submit_result = EDSConnectService(session, _build_test_settings()).submit_form(
        link.raw_token,
        login=login,
        password=password,
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


# ---------------------------------------------------------------------------
# DefaultEDSVerificationAdapter — classifier-level tests
# ---------------------------------------------------------------------------
#
# Regression guard: раньше адаптер классифицировал исключения только по
# подстроке в ``str(exc)``. ``httpx.ReadTimeout`` часто приходит с пустым
# текстом, поэтому попадал в ``verification_unknown_failed`` (non-retryable
# после RETRY_LIMITS=1) вместо ``verification_temporary_failed`` (retryable).
# Реальный прогон на продакшене упал именно на этом: одна попытка → failed,
# пользователь видел "Не удалось завершить подключение из-за технической
# ошибки" без шанса на retry.


def test_failed_connect_log_path_writes_json_line(monkeypatch, tmp_path) -> None:
    """Regression guard: когда ``SREDA_FAILED_CONNECT_LOG_PATH`` задан,
    каждая неудачная попытка подключения должна записывать одну строку
    JSON с метаданными для post-mortem разбора (тип исходного
    exception, login_masked, error_code, account_role, attempts)."""

    log_file = tmp_path / "failed-connect.log"
    monkeypatch.setenv("SREDA_FAILED_CONNECT_LOG_PATH", str(log_file))
    session = _build_session(monkeypatch, tmp_path)
    try:
        job_id = _seed_submitted_connect(session)
        service = EDSAccountVerificationService(
            session,
            telegram_client=FakeTelegramClient(),
            adapter=AuthFailAdapter(),
        )
        asyncio.run(service.process_job(job_id))
    finally:
        session.close()

    assert log_file.exists(), "failed-connect log file must be created"
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["error_code"] == "verification_auth_failed"
    assert record["account_role"] == "primary"
    assert record["login_masked"] == "***41"
    assert record["attempts"] == 1
    assert record["tenant_id"] == "tenant_1"
    assert "ts" in record
    assert "connect_session_id" in record


def _install_leak_client(monkeypatch, side_effect):
    """Подменяет ``EDSMonitorClient`` так, чтобы ``login`` поднял
    переданный exception. ``precheck_credentials`` замещается no-op,
    чтобы проверить именно classification login-ветки (в отдельных
    тестах можно заменить precheck на нужный side_effect)."""

    from sreda_feature_eds_monitor.integrations.client import EDSMonitorClient

    async def _noop(self, *args, **kwargs):
        return None

    async def _fail(self, *args, **kwargs):
        raise side_effect

    monkeypatch.setattr(EDSMonitorClient, "precheck_credentials", _noop)
    monkeypatch.setattr(EDSMonitorClient, "login", _fail)


def test_adapter_maps_httpx_readtimeout_to_retryable_temporary_failed(monkeypatch) -> None:
    import httpx

    from sreda.services.eds_account_verification import (
        DefaultEDSVerificationAdapter,
        VerificationError,
    )

    _install_leak_client(monkeypatch, httpx.ReadTimeout(""))

    adapter = DefaultEDSVerificationAdapter()
    try:
        asyncio.run(
            adapter.verify_account(account_key="teds_x", login="5047136341", password="p")
        )
    except VerificationError as exc:
        assert exc.code == "verification_temporary_failed"
        assert exc.retryable is True
    else:
        raise AssertionError("VerificationError was expected")


def test_adapter_maps_asyncio_timeout_to_retryable_temporary_failed(monkeypatch) -> None:
    from sreda.services.eds_account_verification import (
        DefaultEDSVerificationAdapter,
        VerificationError,
    )

    _install_leak_client(monkeypatch, asyncio.TimeoutError())

    adapter = DefaultEDSVerificationAdapter()
    try:
        asyncio.run(
            adapter.verify_account(account_key="teds_x", login="5047136341", password="p")
        )
    except VerificationError as exc:
        assert exc.code == "verification_temporary_failed"
        assert exc.retryable is True
    else:
        raise AssertionError("VerificationError was expected")


def test_adapter_maps_http_401_to_non_retryable_auth_failed(monkeypatch) -> None:
    import httpx

    from sreda.services.eds_account_verification import (
        DefaultEDSVerificationAdapter,
        VerificationError,
    )

    request = httpx.Request("POST", "https://eds.mosreg.ru/api/login")
    response = httpx.Response(401, request=request)
    _install_leak_client(
        monkeypatch,
        httpx.HTTPStatusError("401 Unauthorized", request=request, response=response),
    )

    adapter = DefaultEDSVerificationAdapter()
    try:
        asyncio.run(
            adapter.verify_account(account_key="teds_x", login="5047136341", password="p")
        )
    except VerificationError as exc:
        assert exc.code == "verification_auth_failed"
        assert exc.retryable is False
    else:
        raise AssertionError("VerificationError was expected")
