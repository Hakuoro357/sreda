import asyncio
import base64
import json
from pathlib import Path


from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    AgentRun,
    AgentThread,
    Assistant,
    Job,
    OutboxMessage,
    Tenant,
    TenantFeature,
    User,
    Workspace,
)
from sreda.db.session import get_engine, get_session_factory
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.executor import ActionRuntimeService


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.edited_messages: list[dict] = []

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

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> dict:
        self.edited_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
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
        # #181 Phase B: /claim is a retired eds_monitor command. The EDS data
        # models are gone, so there is no claim state to seed — the handler is a
        # hardcoded tombstone regardless of any stored state.
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

    # #181: /claim is an eds_monitor feature — now a tombstone. The run still
    # completes (handler answers, no error) but the reply is a disabled notice,
    # NOT a claim card.
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert len(outbox) == 1
    assert len(telegram_client.sent_messages) == 1
    assert "Это умение отключено." in telegram_client.sent_messages[0]["text"]
    assert "Заявка #6230173" not in telegram_client.sent_messages[0]["text"]


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

    # #181: /claim is an eds_monitor feature — tombstoned. With the skill
    # retired, the legacy "claim_id_missing" usage-hint validation no longer
    # fires: policy bypasses the EDS-specific checks and the handler tombstone
    # short-circuits BEFORE reading claim_id, so an empty /claim now completes
    # with the disabled notice (not a failed claim_id_missing run).
    assert runs[0].status == "completed"
    assert "Это умение отключено." in telegram_client.sent_messages[0]["text"]
    assert "Используй команду" not in telegram_client.sent_messages[0]["text"]


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

        # Inject a leaky runtime error from inside a handler to simulate
        # an upstream skill that embedded credentials into its message.
        # After the Phase 1 refactor handlers live in a module-level
        # registry; monkeypatching that dict is the supported
        # injection point.
        from sreda.runtime.executor import ActionRuntimeError
        from sreda.runtime.handlers import HANDLERS

        leaky_message = (
            "Сбой в обработчике (пароль: hunter2-PROD, email test@example.com)."
        )

        def _leaky_handler(_session, _action, _context):
            raise ActionRuntimeError("runtime_unexpected_error", leaky_message)

        monkeypatch.setitem(HANDLERS, "help.show", _leaky_handler)

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


def test_runtime_error_reply_edits_ack_when_ack_progress_enabled(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "runtime_error_ack.db"
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

        from sreda.services.ack_progress import TelegramAckProgressController

        ack_progress = TelegramAckProgressController(
            telegram_client=telegram_client,
            chat_id="100000003",
            ack_message_id_future=321,
            enabled=True,
        )
        service = ActionRuntimeService(
            session,
            telegram_client=telegram_client,
            ack_progress_controller=ack_progress,
        )

        from sreda.runtime.executor import ActionRuntimeError
        from sreda.runtime.handlers import HANDLERS

        def _leaky_handler(_session, _action, _context):
            raise ActionRuntimeError("runtime_unexpected_error", "Не смогла подтвердить действие.")

        monkeypatch.setitem(HANDLERS, "help.show", _leaky_handler)

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
    finally:
        session.close()

    assert result == "failed"
    assert telegram_client.sent_messages == []
    assert telegram_client.edited_messages == [{
        "chat_id": "100000003",
        "message_id": 321,
        "text": "Не смогла подтвердить действие.",
        "reply_markup": None,
    }]


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
