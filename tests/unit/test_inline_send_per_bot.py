"""Tests for per-bot inline-send routing (Fix B).

Verifies that:
  - ``_telegram_for`` resolves the correct client per bot_key when a
    registry is present, and falls back to the single injected client
    when no registry is available.
  - ``ActionRuntimeService`` with ``bot_registry`` set routes a turn's
    inline send via the correct per-bot client.
  - ``ActionRuntimeService`` with ``bot_registry=None`` (legacy / test
    path) falls back to the injected single client unchanged.
"""
from __future__ import annotations

import asyncio
import base64

from sreda.config.bot_registry import BotConfig, TelegramBotRegistry
from sreda.runtime.graph import _telegram_for


# ---------------------------------------------------------------------------
# Fake clients + registry helpers
# ---------------------------------------------------------------------------

class FakeTelegramClient:
    def __init__(self, name: str = "default") -> None:
        self.name = name
        self.sent_messages: list[dict] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return {"ok": True, "result": {"message_id": 1, "date": 1000}}


def _make_registry() -> TelegramBotRegistry:
    """Two-bot registry used across tests.  Tokens are fake test-only strings."""
    return TelegramBotRegistry(
        [
            BotConfig(key="sreda", token="token_sreda_test"),
            BotConfig(key="sreda_home", token="token_sreda_home_test"),
        ],
        system_default_bot_key="sreda",
        admin_bot_key="sreda",
    )


# ---------------------------------------------------------------------------
# _telegram_for unit tests
# ---------------------------------------------------------------------------

def test_telegram_for_with_registry_returns_per_bot_client(monkeypatch) -> None:
    """When a registry is in config, _telegram_for builds a client for that key."""
    registry = _make_registry()

    # Monkeypatch TelegramClient so we don't need a real token.
    from sreda.integrations.telegram import client as tg_module

    created: list[str] = []

    class _FakeClient:
        def __init__(self, token: str) -> None:
            created.append(token)

    monkeypatch.setattr(tg_module, "TelegramClient", _FakeClient)

    config = {"configurable": {"bot_registry": registry, "telegram_client": None}}

    _telegram_for(config, "sreda")
    _telegram_for(config, "sreda_home")

    assert created == ["token_sreda_test", "token_sreda_home_test"]


def test_telegram_for_without_registry_falls_back_to_single_client() -> None:
    """When no registry in config, _telegram_for returns the single injected client."""
    single_client = FakeTelegramClient(name="single")
    config = {"configurable": {"telegram_client": single_client}}

    result = _telegram_for(config, "sreda")
    assert result is single_client

    result2 = _telegram_for(config, "sreda_home")
    assert result2 is single_client


def test_telegram_for_without_registry_and_no_client_returns_none() -> None:
    config = {"configurable": {}}
    assert _telegram_for(config, "sreda") is None


# ---------------------------------------------------------------------------
# ActionRuntimeService executor-level tests
# ---------------------------------------------------------------------------

def _make_db(monkeypatch, tmp_path):
    """Set up a fresh SQLite DB and return (session, engine)."""
    from sreda.config.settings import get_settings
    from sreda.db.base import Base
    from sreda.db.session import get_engine, get_session_factory
    import sreda.db.models  # noqa: F401
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.checklists  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401
    import sreda.db.models.reply_buttons  # noqa: F401

    db_path = tmp_path / "inline_per_bot.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    return session


def _seed_db(session) -> None:
    from sreda.db.models import Assistant, Tenant, User, Workspace

    session.add(Tenant(id="tenant_1", name="Tenant 1"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
    session.flush()
    session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
    session.commit()


def _make_action(bot_key: str):
    from sreda.runtime.dispatcher import ActionEnvelope

    return ActionEnvelope(
        action_type="help.show",
        tenant_id="tenant_1",
        workspace_id="workspace_1",
        assistant_id="assistant_1",
        user_id="user_1",
        channel_type="telegram_dm",
        external_chat_id="100000003",
        bot_key=bot_key,
        inbound_message_id=None,
        source_type="telegram_message",
        source_value="/help",
        params={},
    )


def _patch_telegram_client_for(monkeypatch, sreda_client, sreda_home_client) -> None:
    """Patch ``telegram_client_for`` in all module namespaces that imported it.

    Both ``graph.py`` and ``executor.py`` do ``from sreda.config.bot_registry
    import telegram_client_for``, so they hold their own name references.
    We must patch each module's namespace directly.
    """
    import sreda.runtime.graph as graph_module
    import sreda.runtime.executor as ex_module

    def _fake(bot_key: str, registry):
        return sreda_client if bot_key == "sreda" else sreda_home_client

    monkeypatch.setattr(graph_module, "telegram_client_for", _fake)
    monkeypatch.setattr(ex_module, "telegram_client_for", _fake)


def test_executor_inline_send_uses_per_bot_client_sreda(monkeypatch, tmp_path) -> None:
    """A turn with bot_key='sreda' sends via the sreda client."""
    session = _make_db(monkeypatch, tmp_path)
    try:
        _seed_db(session)

        sreda_client = FakeTelegramClient("sreda")
        sreda_home_client = FakeTelegramClient("sreda_home")
        _patch_telegram_client_for(monkeypatch, sreda_client, sreda_home_client)

        registry = _make_registry()
        from sreda.runtime.executor import ActionRuntimeService

        service = ActionRuntimeService(
            session,
            telegram_client=sreda_client,  # default fallback (shouldn't be used with registry)
            bot_registry=registry,
        )
        queued = service.enqueue_action(_make_action("sreda"))
        asyncio.run(service.process_job(queued.job_id))
    finally:
        session.close()

    assert len(sreda_client.sent_messages) == 1
    assert len(sreda_home_client.sent_messages) == 0


def test_executor_inline_send_uses_per_bot_client_sreda_home(monkeypatch, tmp_path) -> None:
    """A turn with bot_key='sreda_home' sends via the sreda_home client."""
    session = _make_db(monkeypatch, tmp_path)
    try:
        _seed_db(session)

        sreda_client = FakeTelegramClient("sreda")
        sreda_home_client = FakeTelegramClient("sreda_home")
        _patch_telegram_client_for(monkeypatch, sreda_client, sreda_home_client)

        registry = _make_registry()
        from sreda.runtime.executor import ActionRuntimeService

        service = ActionRuntimeService(
            session,
            telegram_client=sreda_client,  # default fallback (shouldn't be used with registry)
            bot_registry=registry,
        )
        queued = service.enqueue_action(_make_action("sreda_home"))
        asyncio.run(service.process_job(queued.job_id))
    finally:
        session.close()

    assert len(sreda_home_client.sent_messages) == 1
    assert len(sreda_client.sent_messages) == 0


def test_executor_inline_send_no_registry_uses_injected_client(monkeypatch, tmp_path) -> None:
    """When bot_registry=None (legacy path), the injected single client is used."""
    session = _make_db(monkeypatch, tmp_path)
    try:
        _seed_db(session)

        single_client = FakeTelegramClient("single")
        from sreda.runtime.executor import ActionRuntimeService

        service = ActionRuntimeService(
            session,
            telegram_client=single_client,
            bot_registry=None,
        )
        queued = service.enqueue_action(_make_action("sreda"))
        asyncio.run(service.process_job(queued.job_id))
    finally:
        session.close()

    assert len(single_client.sent_messages) == 1
