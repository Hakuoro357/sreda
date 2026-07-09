"""Phase 5: bot_key origin in family_reminders + proactive outbox routing.

Tests:
1. schedule_reminder writes bot_key from the turn context (via build_housewife_tools).
2. A legacy reminder (NULL / "sreda") still routes to "sreda".
3. HousewifeReminderWorker emits outbox row with bot_key from reminder.bot_key.
4. TaskService._attach_reminder_inner preserves the turn's bot_key.
5. enforcement test passes with NO opt-out markers (tested separately in
   test_outbox_bot_key_enforcement.py — we just verify the 4 workers here).
6. Migration: family_reminders.bot_key column exists in SQLite schema.
7. Migration: outbox_messages.bot_key is NOT NULL in SQLite schema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.config.bot_registry import LEGACY_NULL_BOT_KEY
from sreda.db.base import Base

# Register all tables (mirrors conftest.py:243-257 pattern).
import sreda.db.models  # noqa: F401
import sreda.db.models.audit  # noqa: F401
import sreda.db.models.checklists  # noqa: F401
import sreda.db.models.free_tier  # noqa: F401
import sreda.db.models.reply_buttons  # noqa: F401

from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.db.models.housewife import FamilyReminder
from sreda.services.housewife_reminders import HousewifeReminderService


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine):
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(Workspace(id="w1", tenant_id="t1", name="W"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _future_dt() -> datetime:
    from datetime import timedelta
    return _utcnow().replace(second=0, microsecond=0) + timedelta(hours=1)


# ---------------------------------------------------------------------------
# 1. schedule_reminder writes bot_key from turn context
# ---------------------------------------------------------------------------

def test_schedule_stores_bot_key(session):
    """HousewifeReminderService.schedule stores the supplied bot_key."""
    svc = HousewifeReminderService(session)
    reminder = svc.schedule(
        tenant_id="t1",
        user_id="u1",
        title="Тест бот-key",
        trigger_at=_future_dt(),
        bot_key="sreda_home",
    )
    assert reminder.bot_key == "sreda_home"


def test_schedule_defaults_to_legacy_bot_key(session):
    """schedule() with bot_key=None falls back to LEGACY_NULL_BOT_KEY."""
    svc = HousewifeReminderService(session)
    reminder = svc.schedule(
        tenant_id="t1",
        user_id="u1",
        title="Легаси напоминание",
        trigger_at=_future_dt(),
        bot_key=None,
    )
    assert reminder.bot_key == LEGACY_NULL_BOT_KEY


# ---------------------------------------------------------------------------
# 2. Reminder worker emits outbox row with reminder.bot_key
# ---------------------------------------------------------------------------

def _seed_worker_base(session) -> None:
    """#138 Ф2: базовый tenant/workspace/user для воркер-тестов на ``worker_db``.

    Раньше эти строки давала фикстура ``session``; воркер теперь открывает свои
    seam-сессии (privileged/tenant), поэтому сид кладём в общую ``worker_db`` БД
    и коммитим ДО прогона воркера (воркер читает своим соединением).
    """
    session.add(Tenant(id="t1", name="T"))
    session.add(Workspace(id="w1", tenant_id="t1", name="W"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    session.commit()


def _make_reminder(session, *, bot_key: str | None) -> FamilyReminder:
    """Insert a due FamilyReminder directly (bypass service to test worker)."""
    from datetime import timedelta
    now = _utcnow()
    past = now - timedelta(minutes=5)
    rem = FamilyReminder(
        id=f"rem_{uuid4().hex[:16]}",
        tenant_id="t1",
        user_id="u1",
        title="Напоминание воркера",
        trigger_at=past,
        next_trigger_at=past,
        status="pending",
        bot_key=bot_key,
    )
    session.add(rem)
    session.commit()  # #138: воркер читает своим соединением → сид коммитим
    return rem


@pytest.mark.asyncio
async def test_reminder_worker_sets_bot_key_from_reminder(worker_db):
    """HousewifeReminderWorker copies reminder.bot_key to outbox row."""
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    _seed_worker_base(worker_db)
    _make_reminder(worker_db, bot_key="sreda_home")
    await HousewifeReminderWorker().process_pending()

    # Outbox — НОВАЯ строка (создана сессией воркера, закоммичена); перечитываем
    # через worker_db после воркера (expire_all для консистентности identity map).
    worker_db.expire_all()
    outbox_rows = worker_db.query(OutboxMessage).all()
    assert outbox_rows, "No outbox rows were created"
    assert all(r.bot_key == "sreda_home" for r in outbox_rows), (
        f"Expected all outbox rows to have bot_key='sreda_home', "
        f"got: {[r.bot_key for r in outbox_rows]}"
    )


@pytest.mark.asyncio
async def test_reminder_worker_legacy_reminder_uses_legacy_bot_key(worker_db):
    """A legacy reminder (bot_key='sreda') routes via 'sreda'."""
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    _seed_worker_base(worker_db)
    _make_reminder(worker_db, bot_key="sreda")
    await HousewifeReminderWorker().process_pending()

    worker_db.expire_all()
    outbox_rows = worker_db.query(OutboxMessage).all()
    assert outbox_rows, "No outbox rows were created"
    assert all(r.bot_key == LEGACY_NULL_BOT_KEY for r in outbox_rows), (
        f"Expected bot_key={LEGACY_NULL_BOT_KEY!r}, got: {[r.bot_key for r in outbox_rows]}"
    )


@pytest.mark.asyncio
async def test_reminder_worker_legacy_bot_key_reminder_routes_to_legacy(worker_db):
    """A reminder with bot_key=LEGACY_NULL_BOT_KEY routes via the legacy bot.

    After migration 20260603_0053, family_reminders.bot_key is NOT NULL so
    true NULL rows can no longer exist in the DB. The equivalent post-migration
    case is a row with bot_key=LEGACY_NULL_BOT_KEY ('sreda'), which is what
    the migration 0051 backfill wrote for all pre-existing rows.
    """
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    _seed_worker_base(worker_db)
    _make_reminder(worker_db, bot_key=LEGACY_NULL_BOT_KEY)

    await HousewifeReminderWorker().process_pending()

    worker_db.expire_all()
    outbox_rows = worker_db.query(OutboxMessage).all()
    assert outbox_rows, "No outbox rows were created"
    assert all(r.bot_key == LEGACY_NULL_BOT_KEY for r in outbox_rows), (
        f"Expected bot_key={LEGACY_NULL_BOT_KEY!r} for legacy reminder, "
        f"got: {[r.bot_key for r in outbox_rows]}"
    )


# ---------------------------------------------------------------------------
# 3. TaskService._attach_reminder_inner preserves bot_key
# ---------------------------------------------------------------------------

def _make_task(session):
    """Create a minimal Task row with schedule + time for attach_reminder."""
    from datetime import date, time
    from sreda.db.models.tasks import Task
    task = Task(
        id=f"task_{uuid4().hex[:16]}",
        tenant_id="t1",
        user_id="u1",
        title="Задача с напоминанием",
        scheduled_date=date(2026, 12, 31),
        time_start=time(10, 0),
        status="pending",
    )
    session.add(task)
    session.commit()
    return task


def test_attach_reminder_preserves_bot_key(session):
    """TaskService._attach_reminder_inner stores the turn's bot_key on the reminder."""
    from sreda.services.tasks import TaskService
    task = _make_task(session)
    svc = TaskService(session, bot_key="sreda_home")
    svc._attach_reminder_inner(task=task, offset_minutes=0)

    reminder = session.get(FamilyReminder, task.reminder_id)
    assert reminder is not None
    assert reminder.bot_key == "sreda_home"


def test_attach_reminder_no_bot_key_uses_legacy(session):
    """TaskService without bot_key stores LEGACY_NULL_BOT_KEY on the reminder."""
    from sreda.services.tasks import TaskService
    task = _make_task(session)
    svc = TaskService(session, bot_key=None)
    svc._attach_reminder_inner(task=task, offset_minutes=0)

    reminder = session.get(FamilyReminder, task.reminder_id)
    assert reminder is not None
    assert reminder.bot_key == LEGACY_NULL_BOT_KEY


# ---------------------------------------------------------------------------
# 4. Schema verification: family_reminders.bot_key column exists
#    and outbox_messages.bot_key is NOT NULL
# ---------------------------------------------------------------------------

def test_family_reminders_has_bot_key_column(engine):
    """family_reminders table must have a NOT NULL bot_key column after migration 0053."""
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("family_reminders")}
    assert "bot_key" in cols, (
        "family_reminders.bot_key column is missing — "
        "check FamilyReminder model and migration 20260603_0053"
    )
    # Column is NOT NULL after migration 20260603_0053.
    assert cols["bot_key"]["nullable"] is False, (
        "family_reminders.bot_key must be NOT NULL — "
        "check FamilyReminder model and migration 20260603_0053"
    )


def test_outbox_messages_bot_key_not_null(engine):
    """outbox_messages.bot_key must be NOT NULL after Phase 5 migration model."""
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("outbox_messages")}
    assert "bot_key" in cols, "outbox_messages.bot_key column is missing"
    assert cols["bot_key"]["nullable"] is False, (
        "outbox_messages.bot_key must be NOT NULL — "
        "check OutboxMessage model and migration 20260603_0052"
    )
