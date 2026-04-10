import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    AgentRun,
    AgentThread,
    Assistant,
    EDSAccount,
    EDSChangeEvent,
    EDSClaimState,
    Job,
    OutboxMessage,
    Tenant,
    TenantFeature,
    User,
    Workspace,
)
from sreda.db.models.billing import TenantSubscription
from sreda.db.session import get_engine, get_session_factory
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.executor import ActionRuntimeService


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True}


def test_runtime_service_reuses_thread_and_sends_outbox(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
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
        session.flush()
        session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
        session.commit()

        telegram_client = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram_client)

        first = service.enqueue_action(
            ActionEnvelope(
                action_type="help.show",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id="100000003",
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_message",
                source_value="/help",
                params={},
            )
        )
        second = service.enqueue_action(
            ActionEnvelope(
                action_type="status.show",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id="100000003",
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_callback",
                source_value="billing:status",
                params={},
            )
        )

        asyncio.run(service.process_job(first.job_id))
        asyncio.run(service.process_job(second.job_id))

        threads = session.query(AgentThread).all()
        runs = session.query(AgentRun).order_by(AgentRun.created_at.asc()).all()
        jobs = session.query(Job).filter(Job.job_type == "agent.execute_action").order_by(Job.id.asc()).all()
        outbox = session.query(OutboxMessage).order_by(OutboxMessage.id.asc()).all()
    finally:
        session.close()

    assert len(threads) == 1
    assert len(runs) == 2
    assert len(jobs) == 2
    assert len(outbox) == 2
    assert runs[0].thread_id == runs[1].thread_id
    assert all(run.status == "completed" for run in runs)
    assert all(job.status == "completed" for job in jobs)
    assert all(message.status == "sent" for message in outbox)
    assert len(telegram_client.sent_messages) == 2
    assert "Я Среда" in telegram_client.sent_messages[0]["text"]
    assert "Мой статус" in telegram_client.sent_messages[1]["text"]
    result_json = json.loads(runs[1].result_json)
    assert result_json["outbox_statuses"] == ["sent"]


def test_runtime_service_add_eds_sends_subscription_and_connect_messages(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "runtime_mutation.db"
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
        session.add(Tenant(id="tenant_1", name="Tenant 1"))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
        session.flush()
        session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
        session.commit()

        telegram_client = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram_client)

        base = service.enqueue_action(
            ActionEnvelope(
                action_type="subscription.connect_base",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id="100000003",
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_callback",
                source_value="billing:connect_plan:eds_monitor_base",
                params={},
            )
        )
        asyncio.run(service.process_job(base.job_id))

        extra = service.enqueue_action(
            ActionEnvelope(
                action_type="subscription.add_eds",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id="100000003",
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_callback",
                source_value="billing:add_eds_account",
                params={},
            )
        )
        asyncio.run(service.process_job(extra.job_id))

        subscriptions = session.query(TenantSubscription).all()
        outbox = session.query(OutboxMessage).order_by(OutboxMessage.id.asc()).all()
    finally:
        session.close()

    assert len(subscriptions) == 2
    assert len(telegram_client.sent_messages) == 3
    assert "Подписка EDS Monitor подключена." in telegram_client.sent_messages[0]["text"]
    assert "Дополнительный кабинет EDS подключен." in telegram_client.sent_messages[1]["text"]
    assert "защищенная одноразовая страница" in telegram_client.sent_messages[2]["text"]
    open_button = telegram_client.sent_messages[2]["reply_markup"]["inline_keyboard"][0][0]
    assert open_button["text"] == "Ввести логин и пароль от EDS"
    assert "web_app" in open_button or "url" in open_button
    assert len(outbox) == 3


def test_runtime_service_claim_lookup_sends_claim_card(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "runtime_claim.db"
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
        session.flush()
        session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
        session.add(TenantFeature(id="feature_1", tenant_id="tenant_1", feature_key="eds_monitor", enabled=True))
        session.add(
            EDSAccount(
                id="eds_acc_1",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                tenant_eds_account_id=None,
                site_key="mosreg",
                account_key="eds-1",
                label="EDS кабинет 1",
                login="5047136341",
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
                updated_at=datetime(2026, 3, 28, 15, 10, tzinfo=UTC),
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
                created_at=datetime(2026, 3, 28, 15, 11, tzinfo=UTC),
            )
        )
        session.commit()

        telegram_client = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram_client)

        queued = service.enqueue_action(
            ActionEnvelope(
                action_type="claim.lookup",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id="100000003",
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_message",
                source_value="/claim 6230173",
                params={"claim_id": "6230173"},
            )
        )
        asyncio.run(service.process_job(queued.job_id))

        runs = session.query(AgentRun).all()
        outbox = session.query(OutboxMessage).all()
    finally:
        session.close()

    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert len(outbox) == 1
    assert len(telegram_client.sent_messages) == 1
    assert "Заявка #6230173" in telegram_client.sent_messages[0]["text"]
    assert "Статус: В работе" in telegram_client.sent_messages[0]["text"]
    assert "Источник: EDS кабинет 1" in telegram_client.sent_messages[0]["text"]


def test_runtime_service_claim_lookup_requires_claim_id(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "runtime_claim_missing.db"
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
        session.flush()
        session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
        session.add(TenantFeature(id="feature_1", tenant_id="tenant_1", feature_key="eds_monitor", enabled=True))
        session.commit()

        telegram_client = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram_client)

        queued = service.enqueue_action(
            ActionEnvelope(
                action_type="claim.lookup",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id="100000003",
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_message",
                source_value="/claim",
                params={},
            )
        )
        asyncio.run(service.process_job(queued.job_id))

        runs = session.query(AgentRun).all()
    finally:
        session.close()

    assert runs[0].status == "failed"
    assert runs[0].error_code == "claim_id_missing"
    assert "Используй команду" in telegram_client.sent_messages[0]["text"]


def test_runtime_mark_failed_sanitizes_error_message_before_persisting(monkeypatch, tmp_path: Path) -> None:
    """Regression guard for H8: an ``ActionRuntimeError.message`` that
    accidentally embeds credentials must be scrubbed before being
    written to ``AgentRun.error_message_sanitized`` or echoed back to
    the user via the Telegram outbox.

    We trigger the real ``claim_id_invalid`` error path with a crafted
    claim id carrying a password-shaped substring — the sanitizer must
    redact it in both the DB row and the outbound message.
    """

    db_path = tmp_path / "runtime_sanitize.db"
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
        session.flush()
        session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
        session.add(TenantFeature(id="feature_1", tenant_id="tenant_1", feature_key="eds_monitor", enabled=True))
        session.commit()

        telegram_client = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram_client)

        # Inject a leaky runtime error from inside the router to simulate
        # an upstream handler that embedded credentials into its message.
        from sreda.runtime.executor import ActionRuntimeError

        leaky_message = (
            "Сбой в обработчике (пароль: hunter2-PROD, email test@example.com)."
        )

        def _leaky_route(action_type: str):
            def _handler(action, context):
                raise ActionRuntimeError("runtime_unexpected_error", leaky_message)

            return _handler

        monkeypatch.setattr(service, "_route_action", _leaky_route)

        queued = service.enqueue_action(
            ActionEnvelope(
                action_type="help.show",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id="100000003",
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_message",
                source_value="/help",
                params={},
            )
        )
        result = asyncio.run(service.process_job(queued.job_id))

        run = session.query(AgentRun).filter(AgentRun.id == queued.run_id).one()
        outbox = session.query(OutboxMessage).order_by(OutboxMessage.id.asc()).all()
    finally:
        session.close()

    assert result == "failed"
    assert run.status == "failed"
    # The raw password and email must not survive in the persisted field
    # or in the outbound Telegram message — only the privacy-guard
    # placeholders may remain.
    assert "hunter2-PROD" not in (run.error_message_sanitized or "")
    assert "test@example.com" not in (run.error_message_sanitized or "")
    assert "[password]" in (run.error_message_sanitized or "")
    assert "[email]" in (run.error_message_sanitized or "")

    assert outbox, "failure notification must be recorded in outbox"
    assert "hunter2-PROD" not in telegram_client.sent_messages[0]["text"]
    assert "test@example.com" not in telegram_client.sent_messages[0]["text"]
    assert "[password]" in telegram_client.sent_messages[0]["text"]


class HangingTelegramClient:
    """Telegram client that blocks forever on send. Used to exercise the
    job-level timeout: without ``asyncio.wait_for`` around the outbound
    delivery, a hung upstream would pin the job in ``running`` state
    indefinitely.
    """

    def __init__(self) -> None:
        self.hit_count = 0

    async def send_message(self, chat_id: str, text: str, parse_mode=None, reply_markup=None) -> dict:
        self.hit_count += 1
        # Sleep for far longer than any reasonable test timeout so the
        # wait_for guard is what unblocks us.
        await asyncio.sleep(3600)
        return {"ok": True}


def test_runtime_process_job_fails_fast_when_telegram_send_hangs(monkeypatch, tmp_path: Path) -> None:
    """Regression guard for H4: when the Telegram send call hangs, the
    job must fail within ``SREDA_JOB_MAX_RUNTIME_SECONDS`` instead of
    leaving the row in ``running`` forever.
    """

    db_path = tmp_path / "runtime_timeout.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_JOB_MAX_RUNTIME_SECONDS", "0.25")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        session.add(Tenant(id="tenant_1", name="Tenant 1"))
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
        session.flush()
        session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda"))
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
        session.commit()

        hanging_client = HangingTelegramClient()
        service = ActionRuntimeService(session, telegram_client=hanging_client)

        queued = service.enqueue_action(
            ActionEnvelope(
                action_type="help.show",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                user_id="user_1",
                channel_type="telegram_dm",
                external_chat_id="100000003",
                bot_key="sreda",
                inbound_message_id=None,
                source_type="telegram_message",
                source_value="/help",
                params={},
            )
        )
        result = asyncio.run(service.process_job(queued.job_id))

        job = session.query(Job).filter(Job.id == queued.job_id).one()
        run = session.query(AgentRun).filter(AgentRun.id == queued.run_id).one()
    finally:
        session.close()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()

    assert result == "failed"
    assert job.status == "failed"
    assert run.status == "failed"
    assert run.error_code == "runtime_timeout"
    # ``send_message`` is hit at least once on the happy-path attempt;
    # the failure-notification path either hits it again (and times out
    # again) or is skipped entirely. What matters is that we never get
    # stuck — the test completes under its own pytest-level budget.
    assert hanging_client.hit_count >= 1
