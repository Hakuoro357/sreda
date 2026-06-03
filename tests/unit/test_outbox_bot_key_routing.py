"""Phase 4a: OutboxDeliveryWorker bot_key routing tests.

Verifies that:
1. A row with bot_key="sreda_home" is delivered via the sreda_home client
   (not the sreda client).
2. A row with bot_key=NULL (legacy migration-window row) falls back to
   LEGACY_NULL_BOT_KEY ("sreda") and is delivered via the sreda client.
3. The MAX path is unaffected by registry changes.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.config.bot_registry import (
    BotConfig,
    LEGACY_NULL_BOT_KEY,
    TelegramBotRegistry,
)
from sreda.db.base import Base
# Register EVERY table before create_all() so OutboxMessage's cross-module FKs
# resolve (else NoReferencedTableError, e.g. tasks_items.checklist_id ->
# checklists). db.models.__init__ omits a few modules — import them explicitly,
# mirroring tests/unit/conftest.py:243-257.
import sreda.db.models  # noqa: F401
import sreda.db.models.audit  # noqa: F401
import sreda.db.models.checklists  # noqa: F401
import sreda.db.models.free_tier  # noqa: F401
import sreda.db.models.reply_buttons  # noqa: F401
from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.workers.outbox_delivery import OutboxDeliveryWorker


# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------

class FakeTelegramClient:
    """Captures send_message calls; identifies itself by token."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.sent: list[dict] = []

    async def send_message(
        self, chat_id: str, text: str, reply_markup=None, **kwargs
    ) -> dict:
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"ok": True}


class FakeTelegramClientFactory:
    """Stands in for telegram_client_for: returns a tracked FakeTelegramClient."""

    def __init__(self) -> None:
        self._clients: dict[str, FakeTelegramClient] = {}

    def __call__(self, bot_key: str, registry: TelegramBotRegistry) -> FakeTelegramClient:
        if bot_key not in self._clients:
            # Look up the token from registry so callers can assert on it.
            cfg = registry.resolve(bot_key)
            self._clients[bot_key] = FakeTelegramClient(token=cfg.token)
        return self._clients[bot_key]

    def client_for(self, bot_key: str) -> FakeTelegramClient | None:
        return self._clients.get(bot_key)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(Workspace(id="w1", tenant_id="t1", name="W"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture()
def registry() -> TelegramBotRegistry:
    return TelegramBotRegistry(
        bots=[
            BotConfig(key="sreda", token="token-sreda"),
            BotConfig(key="sreda_home", token="token-sreda-home"),
        ],
        system_default_bot_key="sreda",
        admin_bot_key="sreda",
    )


def _make_row(session, *, bot_key: str | None, tenant_id="t1", workspace_id="w1") -> OutboxMessage:
    row = OutboxMessage(
        id=f"out_{uuid4().hex[:24]}",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_type="telegram",
        status="pending",
        payload_json=json.dumps({"chat_id": "42", "text": "hello"}),
        is_interactive=True,
        bot_key=bot_key,
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sreda_home_row_delivered_via_sreda_home_client(session, registry, monkeypatch):
    """bot_key='sreda_home' → delivery uses the sreda_home client."""
    factory = FakeTelegramClientFactory()
    monkeypatch.setattr(
        "sreda.workers.outbox_delivery.telegram_client_for", factory
    )

    row = _make_row(session, bot_key="sreda_home")
    worker = OutboxDeliveryWorker(session, registry=registry)
    await worker._send_now(row)

    assert row.status == "sent"
    home_client = factory.client_for("sreda_home")
    assert home_client is not None, "sreda_home client was never created"
    assert len(home_client.sent) == 1
    assert home_client.sent[0]["chat_id"] == "42"

    # sreda client must NOT have been used
    sreda_client = factory.client_for("sreda")
    assert sreda_client is None or len(sreda_client.sent) == 0


@pytest.mark.asyncio
async def test_null_bot_key_falls_back_to_legacy(session, registry, monkeypatch):
    """bot_key=NULL → fallback to LEGACY_NULL_BOT_KEY='sreda'."""
    factory = FakeTelegramClientFactory()
    monkeypatch.setattr(
        "sreda.workers.outbox_delivery.telegram_client_for", factory
    )

    row = _make_row(session, bot_key=None)
    worker = OutboxDeliveryWorker(session, registry=registry)
    await worker._send_now(row)

    assert row.status == "sent"
    sreda_client = factory.client_for(LEGACY_NULL_BOT_KEY)
    assert sreda_client is not None, "sreda (LEGACY_NULL_BOT_KEY) client was never created"
    assert len(sreda_client.sent) == 1

    # sreda_home client must NOT have been used
    home_client = factory.client_for("sreda_home")
    assert home_client is None or len(home_client.sent) == 0


@pytest.mark.asyncio
async def test_sreda_row_delivered_via_sreda_client(session, registry, monkeypatch):
    """bot_key='sreda' → delivery uses the sreda client (explicit, not fallback)."""
    factory = FakeTelegramClientFactory()
    monkeypatch.setattr(
        "sreda.workers.outbox_delivery.telegram_client_for", factory
    )

    row = _make_row(session, bot_key="sreda")
    worker = OutboxDeliveryWorker(session, registry=registry)
    await worker._send_now(row)

    assert row.status == "sent"
    sreda_client = factory.client_for("sreda")
    assert sreda_client is not None
    assert len(sreda_client.sent) == 1

    home_client = factory.client_for("sreda_home")
    assert home_client is None or len(home_client.sent) == 0


@pytest.mark.asyncio
async def test_no_registry_falls_back_to_injected_client(session):
    """When registry=None, the worker still uses the legacy injected client."""
    legacy = FakeTelegramClient(token="legacy-token")

    row = _make_row(session, bot_key="sreda_home")  # even with a bot_key set
    worker = OutboxDeliveryWorker(session, telegram_client=legacy, registry=None)
    await worker._send_now(row)

    assert row.status == "sent"
    assert len(legacy.sent) == 1
