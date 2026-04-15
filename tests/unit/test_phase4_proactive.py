"""Phase 4 integration: inbound_event → proactive handler → outbox → delivery.

Drives the full proactive path with a synthetic skill:
  * Register a proactive handler via ``FeatureRegistry``
  * Seed a subscription with a budget
  * Insert an ``InboundEventDraft``
  * Run ``ProactiveEventWorker`` → outbox row appears
  * Run ``OutboxDeliveryWorker`` → Telegram send fires

Also exercises dedup (UNIQUE constraint), quota-gate (skill skipped
when budget exhausted), and no-handler path (event marked skipped).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models import (
    Assistant,
    InboundEvent,
    OutboxMessage,
    Tenant,
    User,
    Workspace,
)
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.repositories.inbound_event import (
    InboundEventDraft,
    InboundEventRepository,
)
from sreda.features.app_registry import get_feature_registry
from sreda.features.registry import FeatureRegistry
from sreda.runtime.handlers import RuntimeReply
from sreda.services.budget import BudgetService
from sreda.workers.outbox_delivery import OutboxDeliveryWorker
from sreda.workers.proactive_events import ProactiveEventContext, ProactiveEventWorker


TEST_FEATURE_KEY = "phase4_stub"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id: str, text: str, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}


def _seed_world(session):
    session.add(Tenant(id="t1", name="T"))
    session.add(Workspace(id="w1", tenant_id="t1", name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id="t1", workspace_id="w1", name="Sreda"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    session.commit()


def _seed_subscription(
    session, *, feature_key: str, credits_quota: int | None = 1_000_000
) -> SubscriptionPlan:
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=f"{feature_key}_basic",
        feature_key=feature_key,
        title=f"{feature_key} basic",
        description="",
        price_rub=500,
        credits_monthly_quota=credits_quota,
    )
    session.add(plan)
    session.flush()
    session.add(
        TenantSubscription(
            id=f"sub_{uuid4().hex[:16]}",
            tenant_id="t1",
            plan_id=plan.id,
            status="active",
            starts_at=datetime.now(timezone.utc) - timedelta(days=1),
            active_until=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    session.commit()
    return plan


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    _seed_world(sess)
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    """Give each test a clean FeatureRegistry so proactive-handler
    registrations from other tests don't collide."""
    fresh = FeatureRegistry()
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry", lambda: fresh
    )
    # Same module is also imported by proactive worker via
    # ``sreda.workers.proactive_events.get_feature_registry``; patch
    # there too because the `from ... import` binding was resolved at
    # module-load time.
    monkeypatch.setattr(
        "sreda.workers.proactive_events.get_feature_registry",
        lambda: fresh,
    )
    return fresh


# ---------------------------------------------------------------------------
# Dedup / draft ingestion
# ---------------------------------------------------------------------------


def test_create_from_draft_dedup_by_external_key(session):
    repo = InboundEventRepository(session)
    first = repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-1",
            relevance_score=0.9,
            payload={"k": 1},
        )
    )
    dup = repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-1",  # same key → dedup
            relevance_score=0.9,
            payload={"k": 2},
        )
    )
    session.commit()
    assert first is not None
    assert dup is None  # second insert rejected
    rows = session.query(InboundEvent).all()
    assert len(rows) == 1


def test_high_relevance_auto_classified(session):
    repo = InboundEventRepository(session)
    row = repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-auto",
            relevance_score=0.95,
        )
    )
    session.commit()
    assert row.status == "classified"
    assert row.classified_at is not None


def test_low_relevance_stays_new(session):
    repo = InboundEventRepository(session)
    row = repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-low",
            relevance_score=0.1,
        )
    )
    session.commit()
    assert row.status == "new"


# ---------------------------------------------------------------------------
# Proactive worker end-to-end
# ---------------------------------------------------------------------------


def test_proactive_handler_writes_outbox(session, _isolate_registry):
    _seed_subscription(session, feature_key=TEST_FEATURE_KEY)

    captured: dict = {}

    def handler(ctx: ProactiveEventContext):
        captured["event_id"] = ctx.event.id
        captured["payload"] = ctx.event_payload
        captured["profile_tz"] = ctx.profile.get("timezone", "UTC")
        return [
            RuntimeReply(
                text=f"Событие: {ctx.event_payload.get('title', '?')}",
                reply_markup=None,
                feature_key=ctx.event.feature_key,
            )
        ]

    _isolate_registry.register_proactive_handler(
        feature_key=TEST_FEATURE_KEY, handler=handler
    )

    repo = InboundEventRepository(session)
    event = repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-ok",
            relevance_score=0.9,
            payload={"title": "Привет из теста"},
        )
    )
    session.commit()

    worker = ProactiveEventWorker(session)
    processed = asyncio.run(worker.process_pending())
    assert processed == 1

    session.refresh(event)
    assert event.status == "consumed"
    assert event.consumed_at is not None
    assert captured["payload"] == {"title": "Привет из теста"}

    outboxes = session.query(OutboxMessage).all()
    assert len(outboxes) == 1
    outbox = outboxes[0]
    assert outbox.feature_key == TEST_FEATURE_KEY
    assert outbox.is_interactive is False
    assert outbox.user_id == "u1"
    assert "Привет из теста" in outbox.payload_json


def test_proactive_no_handler_marks_skipped(session, _isolate_registry):
    _seed_subscription(session, feature_key=TEST_FEATURE_KEY)
    repo = InboundEventRepository(session)
    event = repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-no-handler",
            relevance_score=0.9,
        )
    )
    session.commit()

    worker = ProactiveEventWorker(session)
    asyncio.run(worker.process_pending())

    session.refresh(event)
    assert event.status == "skipped"
    assert event.status_reason == "no_proactive_handler"
    assert session.query(OutboxMessage).count() == 0


def test_proactive_exhausted_quota_skips(session, _isolate_registry):
    # Tiny quota, pre-fill usage to exhaust it
    _seed_subscription(
        session, feature_key=TEST_FEATURE_KEY, credits_quota=100
    )
    BudgetService(session).record_llm_usage(
        tenant_id="t1",
        feature_key=TEST_FEATURE_KEY,
        model="mimo-v2-pro",
        prompt_tokens=100,
        completion_tokens=0,
        run_id="run_seed",
    )
    session.commit()

    called = [0]

    def handler(ctx):
        called[0] += 1
        return [RuntimeReply(text="boom", reply_markup=None)]

    _isolate_registry.register_proactive_handler(
        feature_key=TEST_FEATURE_KEY, handler=handler
    )

    repo = InboundEventRepository(session)
    event = repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-noquota",
            relevance_score=0.9,
        )
    )
    session.commit()

    worker = ProactiveEventWorker(session)
    asyncio.run(worker.process_pending())

    session.refresh(event)
    assert event.status == "skipped"
    assert event.status_reason == "quota_exhausted"
    assert called[0] == 0  # handler never invoked
    assert session.query(OutboxMessage).count() == 0


def test_proactive_unsubscribed_skill_skipped(session, _isolate_registry):
    """No active subscription → quota check fails → event skipped."""

    def handler(ctx):
        return [RuntimeReply(text="should not run", reply_markup=None)]

    _isolate_registry.register_proactive_handler(
        feature_key=TEST_FEATURE_KEY, handler=handler
    )

    repo = InboundEventRepository(session)
    event = repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-nosub",
            relevance_score=0.9,
        )
    )
    session.commit()

    worker = ProactiveEventWorker(session)
    asyncio.run(worker.process_pending())

    session.refresh(event)
    assert event.status == "skipped"
    assert event.status_reason == "quota_exhausted"


# ---------------------------------------------------------------------------
# Full flow: proactive → outbox → delivery → Telegram
# ---------------------------------------------------------------------------


def test_acceptance_end_to_end_proactive_flow(session, _isolate_registry):
    """End-to-end: skill's proactive handler → outbox → delivery → Telegram."""
    _seed_subscription(session, feature_key=TEST_FEATURE_KEY)

    def handler(ctx: ProactiveEventContext):
        return [
            RuntimeReply(
                text=f"⚡ Новое событие: {ctx.event_payload.get('summary')}",
                reply_markup=None,
                feature_key=ctx.event.feature_key,
            )
        ]

    _isolate_registry.register_proactive_handler(
        feature_key=TEST_FEATURE_KEY, handler=handler
    )

    repo = InboundEventRepository(session)
    repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-e2e",
            relevance_score=1.0,
            payload={"summary": "Заявка #42 обновлена"},
        )
    )
    session.commit()

    # Proactive worker → outbox
    proactive = ProactiveEventWorker(session)
    asyncio.run(proactive.process_pending())
    assert session.query(OutboxMessage).count() == 1

    # Delivery worker → Telegram
    telegram = FakeTelegram()
    delivery = OutboxDeliveryWorker(session, telegram_client=telegram)
    asyncio.run(delivery.process_pending_messages())

    assert len(telegram.sent) == 1
    assert "Заявка #42" in telegram.sent[0]["text"]
    assert telegram.sent[0]["chat_id"] == "42"
