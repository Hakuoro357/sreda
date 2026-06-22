"""#163 Фаза 4 (named-WK) — ключ идемпотентности доставки outbox (с каналом+ботом).

Контракт: повтор той же (reminder, fired_trigger, channel) → НЕТ дубль-постановки (1 outbox);
dual TG+MAX (разный канал) — РАЗЛИЧАЮТСЯ (не схлопываются); DB-инвариант: два outbox с одним
idempotency_key → IntegrityError; NULL-ключ (прочие продюсеры) не ограничен (партиал).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker


def _session(*, max_chat: bool = False):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="tenant_1", name="Test"))
    s.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Default"))
    s.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100",
               max_chat_id="200" if max_chat else None))
    s.commit()
    return s


def _outbox(i, *, key=None):
    return OutboxMessage(
        id=f"out_{i}", tenant_id="tenant_1", workspace_id="workspace_1",
        channel_type="telegram", status="pending", payload_json="{}",
        bot_key="sreda", idempotency_key=key)


def test_worker_outbox_idempotency_key_set_163():
    s = _session()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    r = HousewifeReminderService(s).schedule(
        tenant_id="tenant_1", user_id="user_1", title="Молоко",
        trigger_at=now - timedelta(minutes=1))
    asyncio.run(HousewifeReminderWorker(s).process_pending(now=now))
    rows = s.query(OutboxMessage).all()
    assert len(rows) == 1
    key = rows[0].idempotency_key
    assert key, "ключ идемпотентности должен быть выставлен воркером"
    assert key.startswith(f"{r.id}:"), key
    assert f":{rows[0].channel_type}:" in key, "канал должен входить в ключ"


def test_worker_enqueue_dedups_on_refire_163():
    """Повтор _enqueue для того же reminder+fired_trigger → pre-check скипает → 1 строка/канал."""
    s = _session()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    r = HousewifeReminderService(s).schedule(
        tenant_id="tenant_1", user_id="user_1", title="Молоко",
        trigger_at=now - timedelta(minutes=1))
    w = HousewifeReminderWorker(s)
    w._enqueue_outbox_for(r)  # noqa: SLF001
    s.commit()
    w._enqueue_outbox_for(r)  # noqa: SLF001 — тот же reminder+fired_trigger → дедуп
    s.commit()
    assert s.query(OutboxMessage).count() == 1, "повтор-enqueue не должен дублировать доставку"


def test_worker_dual_channel_distinct_keys_163():
    """Dual TG+MAX → ДВЕ строки (разный канал в ключе) — НЕ схлопываются."""
    s = _session(max_chat=True)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    HousewifeReminderService(s).schedule(
        tenant_id="tenant_1", user_id="user_1", title="Молоко",
        trigger_at=now - timedelta(minutes=1))
    asyncio.run(HousewifeReminderWorker(s).process_pending(now=now))
    rows = s.query(OutboxMessage).all()
    assert len(rows) == 2, f"dual TG+MAX → 2 строки: {rows}"
    assert len({row.idempotency_key for row in rows}) == 2, "разные каналы → разные ключи"
    assert len({row.channel_type for row in rows}) == 2


def test_outbox_idempotency_key_unique_index_163():
    """DB-инвариант: два outbox с ОДНИМ idempotency_key → IntegrityError; NULL не ограничен."""
    s = _session()
    s.add(_outbox(1, key="dup-key"))
    s.commit()
    s.add(_outbox(2, key="dup-key"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    # NULL-ключ (прочие продюсеры) — партиал не ограничивает: два NULL → ок
    s.add(_outbox(3, key=None))
    s.add(_outbox(4, key=None))
    s.commit()
    assert s.query(OutboxMessage).filter(OutboxMessage.idempotency_key.is_(None)).count() == 2
