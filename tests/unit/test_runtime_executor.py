import asyncio
import base64
import json
from pathlib import Path

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import AgentRun, AgentThread, Assistant, Job, OutboxMessage, Tenant, User, Workspace
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
