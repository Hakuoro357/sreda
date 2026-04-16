import base64
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models.connect import ConnectSession, TenantEDSAccount
from sreda.db.models.core import Assistant, Job, SecureRecord, Tenant, TenantFeature, User, Workspace
from sreda.db.session import get_engine, get_session_factory
from sreda.integrations.telegram.client import TelegramClient
from sreda.main import create_app
from sreda.services.eds_account_verification import EDSAccountVerificationService
from sreda.services.billing import BillingService
from sreda.services.eds_connect import ConnectSessionError, EDSConnectService
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
    assert "Сейчас откроется защищенная одноразовая страница" in sent_messages[0]["text"]
    assert "сохраняются в системе только в зашифрованном виде" in sent_messages[0]["text"]
    open_button = sent_messages[0]["reply_markup"]["inline_keyboard"][0][0]
    assert open_button["text"] == "Ввести логин и пароль от EDS"
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
    assert "Это защищенная одноразовая страница для подключения личного кабинета EDS." in response.text
    assert 'Введите логин и пароль и нажмите кнопку "Подключить"' in response.text
    assert "Ссылка действует до:" in response.text
    assert "Логин" in response.text
    assert "Пароль" in response.text
    assert 'id="submit-button"' in response.text
    assert 'submitButton.disabled = true' in response.text
    assert 'submitButton.textContent = "Проверяем..."' in response.text


def test_submit_connect_form_stores_secure_payload_and_queues_job(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")
    called_job_ids: list[str] = []

    async def fake_process_job(self, job_id: str) -> str:
        called_job_ids.append(job_id)
        return "completed"

    monkeypatch.setattr(EDSAccountVerificationService, "process_job", fake_process_job)

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
    assert "Кабинет EDS" in response.text  # verified path → title/message упоминают подключённый ЛК

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
    assert tenant_account.login_masked == "***41"
    assert payload["login"] == "5047136341"
    assert payload["password"] == "super-secret"
    assert job.job_type == "eds.verify_account_connect"
    assert called_job_ids == [job.id]


def test_repeat_submit_returns_processing_page_without_duplicates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    called_job_ids: list[str] = []

    async def fake_process_job(self, job_id: str) -> str:
        called_job_ids.append(job_id)
        return "completed"

    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")
    monkeypatch.setattr(EDSAccountVerificationService, "process_job", fake_process_job)

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
    first_response = client.post(
        f"/connect/eds/{token}",
        data={"login": "5047136341", "password": "super-secret"},
    )
    second_response = client.post(
        f"/connect/eds/{token}",
        data={"login": "5047136341", "password": "super-secret"},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert "Проверка уже запущена." in second_response.text

    session = get_session_factory()()
    try:
        assert session.query(ConnectSession).count() == 1
        assert session.query(TenantEDSAccount).count() == 1
        assert session.query(SecureRecord).filter(SecureRecord.record_type == "eds_connect_payload").count() == 1
        assert session.query(Job).count() == 1
    finally:
        session.close()

    assert len(called_job_ids) == 1


def test_connect_callback_uses_existing_legacy_workspace_and_assistant(
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
        session.add(Tenant(id="tenant_eds", name="tenant_eds"))
        session.add(Workspace(id="workspace_eds", tenant_id="tenant_eds", name="workspace_eds"))
        session.add(User(id="user_eds", tenant_id="tenant_eds", telegram_account_id=CHAT_ID))
        session.flush()
        session.add(Assistant(id="assistant_eds", tenant_id="tenant_eds", workspace_id="workspace_eds", name="Среда"))
        session.add(TenantFeature(id="tenant_eds:core_assistant", tenant_id="tenant_eds", feature_key="core_assistant", enabled=True))
        session.commit()
        BillingService(session).start_base_subscription("tenant_eds")
    finally:
        session.close()

    client = TestClient(create_app())
    payload = {
        "update_id": 9102,
        "callback_query": {
            "id": "cb_connect_legacy",
            "data": "onboarding:connect_eds",
            "message": {
                "message_id": 12,
                "chat": {"id": int(CHAT_ID), "type": "private"},
            },
        },
    }

    response = client.post("/webhooks/telegram/sreda", json=payload)

    assert response.status_code == 202
    assert len(answered_callbacks) == 1
    assert len(sent_messages) == 1

    session = get_session_factory()()
    try:
        connect_session = session.query(ConnectSession).one()
    finally:
        session.close()

    assert connect_session.tenant_id == "tenant_eds"
    assert connect_session.workspace_id == "workspace_eds"
    assert connect_session.user_id == "user_eds"


def test_submit_connect_form_rejects_cross_origin_post(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "csrf.db"
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
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    # Credentials MUST NOT reach secure storage when CSRF gate rejects the request.
    session = get_session_factory()()
    try:
        assert session.query(TenantEDSAccount).count() == 0
        assert session.query(SecureRecord).filter(SecureRecord.record_type == "eds_connect_payload").count() == 0
        assert session.query(Job).count() == 0
        # One-time token must remain usable — a cross-origin attempt should not burn it.
        connect_session = session.query(ConnectSession).one()
        assert connect_session.used_at is None
        assert connect_session.status != "submitted"
    finally:
        session.close()


def test_submit_connect_form_accepts_same_origin_post(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "csrf_ok.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")

    async def fake_process_job(self, job_id: str) -> str:
        return "completed"

    monkeypatch.setattr(EDSAccountVerificationService, "process_job", fake_process_job)

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
        headers={"Origin": "https://connect.example.test"},
    )

    assert response.status_code == 200
    assert "Кабинет EDS" in response.text  # verified path → title/message упоминают подключённый ЛК


def test_submit_connect_form_concurrent_requests_create_only_one_account(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "concurrent.db"
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

    # Synchronize two submits so both pass the read-phase validation before
    # either performs the claim — reproducing the concurrent-write race.
    barrier = threading.Barrier(2)
    original_require = EDSConnectService._require_valid_session

    def synchronized_require(self, raw_token):
        cs = original_require(self, raw_token)
        barrier.wait(timeout=10)
        return cs

    monkeypatch.setattr(EDSConnectService, "_require_valid_session", synchronized_require)

    results: dict[int, object] = {}

    def run(n: int) -> None:
        local_session = get_session_factory()()
        try:
            service = EDSConnectService(local_session, get_settings())
            try:
                results[n] = service.submit_form(
                    token, login="5047136341", password="super-secret"
                )
            except ConnectSessionError as exc:
                results[n] = exc
            except Exception as exc:  # pragma: no cover - surfaced via assertions
                results[n] = exc
        finally:
            local_session.close()

    thread_a = threading.Thread(target=run, args=(1,))
    thread_b = threading.Thread(target=run, args=(2,))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    successes = [r for r in results.values() if not isinstance(r, Exception)]
    failures = [r for r in results.values() if isinstance(r, ConnectSessionError)]

    assert len(successes) == 1, f"expected exactly one successful submit, got {results}"
    assert len(failures) == 1, f"expected exactly one rejected submit, got {results}"
    assert failures[0].code == "session_used"

    session = get_session_factory()()
    try:
        assert session.query(ConnectSession).count() == 1
        assert session.query(TenantEDSAccount).count() == 1
        assert session.query(Job).count() == 1
        assert (
            session.query(SecureRecord)
            .filter(SecureRecord.record_type == "eds_connect_payload")
            .count()
            == 1
        )
    finally:
        session.close()


def test_connect_endpoint_rate_limits_excess_requests(monkeypatch, tmp_path: Path) -> None:
    """Regression guard for H1: the public ``/connect/eds/*`` endpoint
    must refuse traffic above the configured per-IP cap with a
    ``429 Too Many Requests`` response. We set a tiny cap via env
    vars, hit the endpoint repeatedly with an invalid token (the
    limiter runs before the token lookup, so even invalid tokens
    trigger the limit), and assert that the last call is rejected.
    """

    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")

    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")
    monkeypatch.setenv("SREDA_RATE_LIMIT_CONNECT_MAX_REQUESTS", "3")
    monkeypatch.setenv("SREDA_RATE_LIMIT_CONNECT_WINDOW_SECONDS", "60")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    from sreda.api.deps import reset_rate_limiters

    reset_rate_limiters()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        _seed_bundle(session)
    finally:
        session.close()

    client = TestClient(create_app())
    try:
        first = client.get("/connect/eds/not-a-real-token")
        second = client.get("/connect/eds/not-a-real-token")
        third = client.get("/connect/eds/not-a-real-token")
        fourth = client.get("/connect/eds/not-a-real-token")
    finally:
        reset_rate_limiters()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()

    # The first three fall through to the service layer (which returns
    # an error page with ``status_code != 429``), the fourth gets shut
    # down by the limiter.
    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code != 429
    assert fourth.status_code == 429
    assert fourth.json()["detail"] == "rate_limited"


def test_submit_form_shows_verified_message_when_inline_verification_completes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Regression guard for M1 follow-up: страница мини-аппа должна
    показывать "Кабинет EDS подключен" только если ``process_job`` вернул
    ``"completed"`` — именно это пользователь видел как ложный success в
    реальном прогоне (job упал с failed, а страница говорила "проверяем").
    """

    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")

    async def fake_process_job(self, job_id: str) -> str:
        return "completed"

    monkeypatch.setattr(EDSAccountVerificationService, "process_job", fake_process_job)
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

    response = TestClient(create_app()).post(
        f"/connect/eds/{token}",
        data={"login": "5047136341", "password": "super-secret"},
    )

    assert response.status_code == 200
    assert "Кабинет EDS подключ" in response.text  # покрывает "подключен/подключён"
    assert "Результат проверки появится" not in response.text


def test_submit_form_shows_neutral_message_when_inline_verification_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Regression guard for M1 follow-up: когда ``process_job`` возвращает
    ``"failed"`` (штатный исход без exception), страница не должна
    утверждать, что кабинет подключён. Должна отдавать нейтральный
    текст "результат появится в приложении".
    """

    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")

    async def fake_process_job(self, job_id: str) -> str:
        return "failed"

    monkeypatch.setattr(EDSAccountVerificationService, "process_job", fake_process_job)
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

    response = TestClient(create_app()).post(
        f"/connect/eds/{token}",
        data={"login": "5047136341", "password": "super-secret"},
    )

    assert response.status_code == 200
    assert "Кабинет EDS подключ" not in response.text
    assert "Результат проверки появится в приложении" in response.text


def test_submit_form_shows_neutral_message_when_inline_verification_raises(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Exception из ``process_job`` — такой же нейтральный исход, как и
    ``"failed"``. Страница не должна обещать успех."""

    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.example.test")

    async def fake_process_job(self, job_id: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(EDSAccountVerificationService, "process_job", fake_process_job)
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

    response = TestClient(create_app()).post(
        f"/connect/eds/{token}",
        data={"login": "5047136341", "password": "super-secret"},
    )

    assert response.status_code == 200
    assert "Кабинет EDS подключ" not in response.text
    assert "Результат проверки появится в приложении" in response.text


def _seed_bundle(session) -> None:
    session.add(Tenant(id="tenant_1", name="Tenant 1"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id=CHAT_ID))
    session.flush()
    session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Среда"))
    session.add(TenantFeature(id="tenant_1:core_assistant", tenant_id="tenant_1", feature_key="core_assistant", enabled=True))
    session.commit()
