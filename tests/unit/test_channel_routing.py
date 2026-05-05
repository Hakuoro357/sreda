"""Tests для services.channel_routing.resolve_outbox_routings (10.6).

Pre-existing producers (housewife_reminder, housewife_onboarding,
onboarding_aha, proactive_events) раньше hardcoded
`channel_type="telegram"`. Теперь они вызывают
`resolve_outbox_routings(session, tenant, user)` которая решает все
доступные channels на основе available account_id'ов.

**Dual delivery (Boris directive 2026-05-05):** если у юзера оба
TG+MAX account'а — функция возвращает list из двух OutboxRouting
(один TG, один MAX). Producers iterate и создают outbox row на
каждый channel — нотификация приходит и в TG и в МАКС.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.services.channel_routing import (
    OutboxRouting,
    resolve_outbox_routing,
    resolve_outbox_routings,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    try:
        yield sess
    finally:
        sess.close()


def _add_user(session, **kw):
    """Helper: создать tenant + user с заданными account_id."""
    tenant_id = kw.pop("tenant_id", "t1")
    preferred = kw.pop("preferred_channel", None)
    if not session.get(Tenant, tenant_id):
        session.add(Tenant(id=tenant_id, name="T", preferred_channel=preferred))
    user = User(id=f"user_{tenant_id}", tenant_id=tenant_id, **kw)
    session.add(user)
    session.commit()
    return session.get(Tenant, tenant_id), user


# ---------------------------------------------------------------------------
# resolve_outbox_routings (plural) — dual-delivery semantics
# ---------------------------------------------------------------------------


def test_routings_user_with_only_telegram(session):
    tenant, user = _add_user(session, telegram_account_id="111", max_account_id=None)
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert routings == [OutboxRouting(channel="telegram", chat_id="111")]


def test_routings_user_with_only_max(session):
    tenant, user = _add_user(
        session, telegram_account_id=None,
        max_account_id="222", max_chat_id="999",
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert routings == [OutboxRouting(channel="max", chat_id="999")]


def test_routings_user_with_both_returns_both_dual_delivery(session):
    """Boris directive: если оба channel'а — нотифицируем в оба."""
    tenant, user = _add_user(
        session, telegram_account_id="111",
        max_account_id="222", max_chat_id="999",
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert len(routings) == 2
    channels = [r.channel for r in routings]
    assert set(channels) == {"telegram", "max"}
    assert channels[0] == "telegram"  # TG first by default order


def test_routings_preferred_max_orders_max_first(session):
    """preferred_channel влияет ТОЛЬКО на порядок, оба остаются."""
    tenant, user = _add_user(
        session, telegram_account_id="111",
        max_account_id="222", max_chat_id="999",
        preferred_channel="max",
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert len(routings) == 2
    assert routings[0].channel == "max"   # primary first
    assert routings[1].channel == "telegram"


def test_routings_user_without_any_account_returns_empty(session):
    tenant, user = _add_user(
        session, telegram_account_id=None, max_account_id=None,
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert routings == []


def test_routings_no_user_returns_empty(session):
    assert resolve_outbox_routings(session, tenant=None, user=None) == []


def test_routings_max_uses_chat_id_not_account_id(session):
    tenant, user = _add_user(
        session, telegram_account_id=None,
        max_account_id="max_account_test",
        max_chat_id="max_chat_test",
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert routings[0].channel == "max"
    assert routings[0].chat_id == "max_chat_test"  # chat_id, not account_id


def test_routings_max_account_without_chat_id_undeliverable(session):
    """Codex R2 MINOR: ``max_account_id`` set но ``max_chat_id=None``
    — НЕ deliverable. Это legacy data ситуация: bot identity известна
    но recipient'а нет."""
    tenant, user = _add_user(
        session, telegram_account_id=None,
        max_account_id="max_account_test", max_chat_id=None,
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert routings == [], (
        "max_account_id без max_chat_id не должен давать MAX routing "
        "(chat_id это recipient в /messages, а не account_id)"
    )


def test_routings_no_tenant_defaults_to_tg_first(session):
    user = User(id="u1", tenant_id="t_orphan",
                telegram_account_id="111", max_account_id="222",
                max_chat_id="999")
    session.add(Tenant(id="t_orphan", name="X"))
    session.add(user)
    session.commit()
    routings = resolve_outbox_routings(session, tenant=None, user=user)
    assert len(routings) == 2
    assert routings[0].channel == "telegram"


# ---------------------------------------------------------------------------
# resolve_outbox_routing (singular, backward-compat) — first routing only
# ---------------------------------------------------------------------------


def test_singular_returns_first_of_dual(session):
    tenant, user = _add_user(
        session, telegram_account_id="111",
        max_account_id="222", max_chat_id="999",
    )
    # Singular shim: returns ONLY first routing (loses second)
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing is not None
    assert routing.channel == "telegram"


def test_singular_returns_none_when_no_accounts(session):
    tenant, user = _add_user(
        session, telegram_account_id=None, max_account_id=None,
    )
    assert resolve_outbox_routing(session, tenant=tenant, user=user) is None


def test_singular_emits_deprecation_warning(session, recwarn):
    """Codex R1 MINOR: shim должен warning'ить что миграция нужна."""
    tenant, user = _add_user(session, telegram_account_id="111")
    resolve_outbox_routing(session, tenant=tenant, user=user)
    deprecations = [w for w in recwarn.list if issubclass(w.category, DeprecationWarning)]
    assert deprecations, "expected DeprecationWarning from singular shim"
    assert "deprecated" in str(deprecations[0].message).lower()


# ---------------------------------------------------------------------------
# Producer-level integration test (codex R1 MAJOR #4): housewife_reminder_worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_housewife_reminder_dual_delivery_creates_two_outbox_rows(session):
    """Юзер с TG+MAX → reminder fire создаёт 2 outbox rows (TG + MAX)."""
    from datetime import datetime, timezone
    from sreda.db.models.core import OutboxMessage, Workspace
    from sreda.db.models.housewife import FamilyReminder
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    tenant, user = _add_user(
        session,
        telegram_account_id="tg_chat_111",
        max_account_id="max_account_test",
        max_chat_id="max_chat_test",
    )
    session.add(Workspace(id="ws_t1", tenant_id=tenant.id, name="WS"))
    now = datetime.now(timezone.utc)
    session.add(FamilyReminder(
        id="rem_dual", tenant_id=tenant.id, user_id=user.id,
        title="Тестовое напоминание",
        trigger_at=now,
        next_trigger_at=now,
        status="pending",
    ))
    session.commit()

    worker = HousewifeReminderWorker(session)
    await worker.process_pending(limit=10, now=now)

    rows = (
        session.query(OutboxMessage)
        .filter(OutboxMessage.tenant_id == tenant.id)
        .order_by(OutboxMessage.channel_type.asc())
        .all()
    )
    assert len(rows) == 2, f"expected 2 outbox rows (TG+MAX), got {len(rows)}"
    channels = sorted([r.channel_type for r in rows])
    assert channels == ["max", "telegram"]


@pytest.mark.asyncio
async def test_housewife_reminder_cross_tenant_user_id_skipped(session, caplog):
    """Codex R2/R5 MAJOR: reminder.user_id указывает на user из ДРУГОГО
    tenant'а → no outbox + warning log + reminder.status='fired'
    (mark_fired чтобы не зацикливаться).

    Защита от случаев когда manual SQL merge / FK snapshot ошибся —
    user_id мог попасть от чужого tenant'а.
    """
    import logging
    from datetime import datetime, timezone
    from sreda.db.models.core import OutboxMessage, Workspace
    from sreda.db.models.housewife import FamilyReminder
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    tenant_this_id = "t_this"
    tenant_other_id = "t_other"
    session.add(Tenant(id=tenant_this_id, name="This"))
    session.add(Tenant(id=tenant_other_id, name="Other"))
    session.add(User(id="cross_user", tenant_id=tenant_other_id,
                     telegram_account_id="cross_tg_chat"))
    session.add(Workspace(id="ws_this", tenant_id=tenant_this_id, name="WS"))
    now = datetime.now(timezone.utc)
    session.add(FamilyReminder(
        id="rem_xtenant", tenant_id=tenant_this_id, user_id="cross_user",
        title="Reminder с FK leak'нувшим в чужого tenant'а",
        trigger_at=now, next_trigger_at=now,
        status="pending",
    ))
    session.commit()

    caplog.set_level(logging.WARNING, logger="sreda.workers.housewife_reminder_worker")
    worker = HousewifeReminderWorker(session)
    fired = await worker.process_pending(limit=10, now=now)

    # 1) No outbox row — leak prevented
    rows = (
        session.query(OutboxMessage)
        .filter(OutboxMessage.tenant_id == tenant_this_id)
        .all()
    )
    assert rows == [], f"no leak; got rows: {rows}"

    # 2) Worker marked reminder fired (чтобы не retry'ило infinitely);
    # delivery silently skipped, but state advanced.
    session.expire_all()
    rem = session.get(FamilyReminder, "rem_xtenant")
    assert rem.status == "fired", f"expected fired, got {rem.status}"
    assert fired == 1  # processed

    # 3) Warning log mentions tenant mismatch — visible в monitor
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("tenant" in m.lower() and "mismatch" in m.lower() for m in warn_msgs), (
        f"expected tenant-mismatch warning, got {warn_msgs}"
    )


@pytest.mark.asyncio
async def test_housewife_onboarding_cross_tenant_user_id_skipped(session, monkeypatch):
    """Codex R4 MAJOR: HousewifeOnboardingKickoffWorker._fire() — если
    user_id указывает на user из другого tenant'а → skip + return False,
    NOT delivery."""
    from sreda.db.models.core import OutboxMessage, Workspace
    from sreda.workers.housewife_onboarding_worker import (
        HousewifeOnboardingKickoffWorker,
    )

    tenant_this_id = "t_oh_this"
    tenant_other_id = "t_oh_other"
    session.add(Tenant(id=tenant_this_id, name="This"))
    session.add(Tenant(id=tenant_other_id, name="Other"))
    session.add(User(id="oh_cross", tenant_id=tenant_other_id,
                     telegram_account_id="oh_cross_tg"))
    session.add(Workspace(id="ws_oh_this", tenant_id=tenant_this_id, name="WS"))
    session.commit()

    # Build worker without triggering full housewife_onboarding service init
    worker = HousewifeOnboardingKickoffWorker.__new__(
        HousewifeOnboardingKickoffWorker,
    )
    worker.session = session
    worker.service = type("S", (), {"start": lambda self, **kw: None})()

    fired = worker._fire(tenant_id=tenant_this_id, user_id="oh_cross")

    assert fired is False, "cross-tenant user_id должен быть skip'нут"
    rows = (
        session.query(OutboxMessage)
        .filter(OutboxMessage.tenant_id == tenant_this_id)
        .all()
    )
    assert rows == [], f"Got rows: {rows}"


@pytest.mark.asyncio
async def test_proactive_events_cross_tenant_user_id_skipped(session):
    """Codex R4 MAJOR: ProactiveEventWorker._resolve_routings — если
    event.user_id указывает на user из другого tenant'а → empty
    routings → skipped event, NO outbox."""
    from sreda.workers.proactive_events import ProactiveEventWorker

    tenant_this_id = "t_pe_this"
    tenant_other_id = "t_pe_other"
    session.add(Tenant(id=tenant_this_id, name="This"))
    session.add(Tenant(id=tenant_other_id, name="Other"))
    session.add(User(id="pe_cross", tenant_id=tenant_other_id,
                     telegram_account_id="pe_cross_tg"))
    session.commit()

    worker = ProactiveEventWorker.__new__(ProactiveEventWorker)
    worker.session = session

    # Mock InboundEvent
    class _FakeEvent:
        tenant_id = tenant_this_id
        user_id = "pe_cross"

    routings = worker._resolve_routings(_FakeEvent())
    assert routings == [], (
        "cross-tenant event.user_id должен дать empty routings — "
        f"got {routings!r}"
    )


@pytest.mark.asyncio
async def test_housewife_reminder_user_scoped_no_fallback_to_other_user(session):
    """Codex R1 CRITICAL: reminder.user_id без accounts → skip,
    NOT fallback на другого user'а tenant'а (личные данные leak)."""
    from datetime import datetime, timezone
    from sreda.db.models.core import OutboxMessage, Workspace
    from sreda.db.models.housewife import FamilyReminder
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    tenant_id = "t_leak"
    session.add(Tenant(id=tenant_id, name="LeakTest"))
    session.add(User(id="user_a", tenant_id=tenant_id,
                     telegram_account_id=None, max_account_id=None))
    session.add(User(id="user_b", tenant_id=tenant_id,
                     telegram_account_id="OTHER_USER_TG"))
    session.add(Workspace(id="ws_leak", tenant_id=tenant_id, name="WS"))
    now = datetime.now(timezone.utc)
    session.add(FamilyReminder(
        id="rem_leak", tenant_id=tenant_id, user_id="user_a",
        title="Личное напоминание для user_a",
        trigger_at=now,
        next_trigger_at=now,
        status="pending",
    ))
    session.commit()

    worker = HousewifeReminderWorker(session)
    await worker.process_pending(limit=10, now=now)

    rows = (
        session.query(OutboxMessage)
        .filter(OutboxMessage.tenant_id == tenant_id)
        .all()
    )
    assert rows == [], (
        "reminder for user_a (no accounts) НЕ ДОЛЖЕН leak'нуться "
        f"в outbox для user_b. Got rows: {[r.payload_json[:50] for r in rows]}"
    )
