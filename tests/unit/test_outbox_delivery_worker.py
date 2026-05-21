"""Phase 2d: OutboxDeliveryWorker end-to-end against SQLite.

Exercises the full path the worker takes: read pending outbox, resolve
per-user profile + skill config, apply delivery policy, dispatch to
telegram / defer / drop. Uses injected ``now`` to control time.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.integrations.max.client import MaxDeliveryError
from sreda.workers.outbox_delivery import OutboxDeliveryWorker


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id: str, text: str, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}


class FakeMax:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[str] = []
        self.edit_failures = 0

    async def send_message(self, *, recipient, text, format=None, attachments=None):
        self.sent.append({
            "recipient": recipient,
            "text": text,
            "format": format,
            "attachments": attachments,
        })
        return {"message": {"body": {"mid": "new_mid"}}}

    async def edit_message(self, message_id, *, text, attachments=None):
        if self.edit_failures > 0:
            self.edit_failures -= 1
            raise MaxDeliveryError("temporary edit failure")
        self.edited.append({
            "message_id": message_id,
            "text": text,
            "attachments": attachments,
        })
        return {"message": {"body": {"mid": message_id}}}

    async def delete_message(self, message_id):
        self.deleted.append(message_id)
        return {"success": True}


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


def _outbox_row(
    *,
    status: str = "pending",
    scheduled_at: datetime | None = None,
    feature_key: str | None = None,
    is_interactive: bool = False,
    user_id: str | None = "u1",
    text: str = "hello",
) -> OutboxMessage:
    return OutboxMessage(
        id=f"out_{uuid4().hex[:16]}",
        tenant_id="t1",
        workspace_id="w1",
        user_id=user_id,
        channel_type="telegram",
        feature_key=feature_key,
        is_interactive=is_interactive,
        status=status,
        scheduled_at=scheduled_at,
        payload_json=json.dumps({"chat_id": "42", "text": text, "reply_markup": None}),
    )


def _max_outbox_row(
    *,
    text: str = "hello",
    ack_message_id: str | None = None,
) -> OutboxMessage:
    payload = {"chat_id": "max-chat", "text": text, "reply_markup": None}
    if ack_message_id:
        payload["_ack_edit_message_id"] = ack_message_id
    return OutboxMessage(
        id=f"out_{uuid4().hex[:16]}",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        channel_type="max",
        feature_key=None,
        is_interactive=True,
        status="pending",
        payload_json=json.dumps(payload),
    )


def _utc(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_worker_sends_pending_interactive_row(session):
    telegram = FakeTelegram()
    worker = OutboxDeliveryWorker(session, telegram_client=telegram)
    row = _outbox_row(is_interactive=True)
    session.add(row)
    session.commit()

    processed = asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 15, 23, 0)))
    assert processed == 1
    session.refresh(row)
    assert row.status == "sent"
    assert len(telegram.sent) == 1


def test_worker_defers_proactive_row_inside_quiet(session):
    repo = UserProfileRepository(session)
    repo.update_profile(
        "t1",
        "u1",
        tz="Europe/Moscow",
        quiet_hours=[{"from_hour": 22, "to_hour": 8, "weekdays": list(range(7))}],
    )
    session.commit()

    telegram = FakeTelegram()
    worker = OutboxDeliveryWorker(session, telegram_client=telegram)
    # Proactive EDS row at 20:00 UTC == 23:00 MSK (inside quiet)
    row = _outbox_row(feature_key="eds_monitor")
    session.add(row)
    session.commit()

    now = _utc(2026, 4, 15, 20, 0)
    processed = asyncio.run(worker.process_pending_messages(now=now))
    assert processed == 1
    session.refresh(row)
    assert row.status == "pending"
    # Expected exit: 08:00 MSK next day == 05:00 UTC 2026-04-16
    # SQLite strips tzinfo on roundtrip — normalize both sides.
    actual = row.scheduled_at
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    assert actual == datetime(2026, 4, 16, 5, 0, tzinfo=timezone.utc)
    assert len(telegram.sent) == 0


def test_worker_drops_muted_proactive_row(session):
    repo = UserProfileRepository(session)
    repo.upsert_skill_config(
        "t1", "u1", "eds_monitor", notification_priority="mute"
    )
    session.commit()

    telegram = FakeTelegram()
    worker = OutboxDeliveryWorker(session, telegram_client=telegram)
    row = _outbox_row(feature_key="eds_monitor")
    session.add(row)
    session.commit()

    processed = asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 15, 12, 0)))
    assert processed == 1
    session.refresh(row)
    assert row.status == "muted"
    assert len(telegram.sent) == 0


def test_worker_urgent_bypasses_quiet(session):
    repo = UserProfileRepository(session)
    repo.update_profile(
        "t1",
        "u1",
        tz="Europe/Moscow",
        quiet_hours=[{"from_hour": 22, "to_hour": 8, "weekdays": list(range(7))}],
    )
    repo.upsert_skill_config(
        "t1", "u1", "eds_monitor", notification_priority="urgent"
    )
    session.commit()

    telegram = FakeTelegram()
    worker = OutboxDeliveryWorker(session, telegram_client=telegram)
    row = _outbox_row(feature_key="eds_monitor")
    session.add(row)
    session.commit()

    now = _utc(2026, 4, 15, 20, 0)  # 23:00 MSK (quiet)
    processed = asyncio.run(worker.process_pending_messages(now=now))
    assert processed == 1
    session.refresh(row)
    assert row.status == "sent"
    assert len(telegram.sent) == 1


def test_worker_ignores_future_scheduled(session):
    telegram = FakeTelegram()
    worker = OutboxDeliveryWorker(session, telegram_client=telegram)
    # Scheduled for tomorrow — should NOT be picked up now
    row = _outbox_row(scheduled_at=_utc(2026, 4, 16, 5, 0))
    session.add(row)
    session.commit()

    processed = asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 15, 20, 0)))
    assert processed == 0
    session.refresh(row)
    assert row.status == "pending"


def test_worker_picks_up_expired_scheduled(session):
    telegram = FakeTelegram()
    worker = OutboxDeliveryWorker(session, telegram_client=telegram)
    # Scheduled for earlier today; now is after — should go out
    row = _outbox_row(scheduled_at=_utc(2026, 4, 16, 5, 0))
    session.add(row)
    session.commit()

    processed = asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 16, 6, 0)))
    assert processed == 1
    session.refresh(row)
    assert row.status == "sent"


def test_worker_defer_then_send_after_quiet(session):
    """Full defer-wake cycle: worker defers at 23:00, then picks it up
    automatically at 09:00 and sends."""
    repo = UserProfileRepository(session)
    repo.update_profile(
        "t1",
        "u1",
        tz="Europe/Moscow",
        quiet_hours=[{"from_hour": 22, "to_hour": 8, "weekdays": list(range(7))}],
    )
    session.commit()

    telegram = FakeTelegram()
    worker = OutboxDeliveryWorker(session, telegram_client=telegram)
    row = _outbox_row(feature_key="eds_monitor")
    session.add(row)
    session.commit()

    # 23:00 MSK — defer
    asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 15, 20, 0)))
    session.refresh(row)
    assert row.status == "pending"
    assert row.scheduled_at is not None

    # Wake at 09:00 MSK (06:00 UTC next day)
    asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 16, 6, 0)))
    session.refresh(row)
    assert row.status == "sent"
    assert len(telegram.sent) == 1


def test_worker_edits_max_ack_message_instead_of_sending_new_message(session):
    max_client = FakeMax()
    worker = OutboxDeliveryWorker(session, max_client=max_client)
    row = _max_outbox_row(text="Финальный ответ", ack_message_id="ack-mid-1")
    session.add(row)
    session.commit()

    processed = asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 15, 23, 0)))

    assert processed == 1
    session.refresh(row)
    assert row.status == "sent"
    assert max_client.sent == []
    assert max_client.edited == [{
        "message_id": "ack-mid-1",
        "text": "Финальный ответ",
        "attachments": None,
    }]


def test_worker_retries_max_ack_edit_then_falls_back_to_send(session):
    max_client = FakeMax()
    max_client.edit_failures = 2
    worker = OutboxDeliveryWorker(session, max_client=max_client)
    row = _max_outbox_row(text="Финальный ответ", ack_message_id="ack-mid-1")
    session.add(row)
    session.commit()

    processed = asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 15, 23, 0)))

    assert processed == 1
    session.refresh(row)
    assert row.status == "sent"
    assert max_client.edited == []
    assert max_client.sent == [{
        "recipient": {"chat_id": "max-chat"},
        "text": "Финальный ответ",
        "format": None,
        "attachments": None,
    }]
    assert max_client.deleted == ["ack-mid-1"]
