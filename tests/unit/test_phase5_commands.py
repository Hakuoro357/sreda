"""Phase 5-lite integration: /throttle, /stats commands + end-to-end
ProactiveEventWorker integration with decide_to_speak."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    Assistant,
    OutboxMessage,
    Tenant,
    TenantUserProfile,
    User,
    Workspace,
)
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.repositories.inbound_event import (
    InboundEventDraft,
    InboundEventRepository,
)
from sreda.db.session import get_engine, get_session_factory
from sreda.features.registry import FeatureRegistry
from sreda.runtime.dispatcher import ActionEnvelope, _resolve_command_action
from sreda.runtime.executor import ActionRuntimeService
from sreda.runtime.handlers import RuntimeReply
from sreda.workers.proactive_events import ProactiveEventContext, ProactiveEventWorker


TEST_FEATURE_KEY = "phase5_stub"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}


def _bootstrap(monkeypatch, tmp_path: Path, name: str):
    db_path = tmp_path / name
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    session.add(Tenant(id="t1", name="T"))
    session.add(Workspace(id="w1", tenant_id="t1", name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id="t1", workspace_id="w1", name="Sreda"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    session.commit()
    return session


def _seed_subscription(session, feature_key: str):
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=f"{feature_key}_basic",
        feature_key=feature_key,
        title="test",
        description="",
        price_rub=500,
        credits_monthly_quota=1_000_000,
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


def _envelope(action_type: str, **params) -> ActionEnvelope:
    return ActionEnvelope(
        action_type=action_type,
        tenant_id="t1",
        workspace_id="w1",
        assistant_id="a1",
        user_id="u1",
        channel_type="telegram_dm",
        external_chat_id="42",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_message",
        source_value="/stub",
        params=params,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_dispatcher_parses_throttle_and_stats():
    assert _resolve_command_action("/throttle") == (
        "profile.set_throttle",
        {"minutes": ""},
    )
    assert _resolve_command_action("/throttle 60") == (
        "profile.set_throttle",
        {"minutes": "60"},
    )
    assert _resolve_command_action("/stats") == ("stats.show", {})


# ---------------------------------------------------------------------------
# /throttle handler
# ---------------------------------------------------------------------------


def test_throttle_sets_profile_minutes(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "p5t1.db")
    try:
        telegram = FakeTelegram()
        svc = ActionRuntimeService(session, telegram_client=telegram)
        queued = svc.enqueue_action(_envelope("profile.set_throttle", minutes="60"))
        asyncio.run(svc.process_job(queued.job_id))
        profile = session.query(TenantUserProfile).one()
    finally:
        session.close()

    assert profile.proactive_throttle_minutes == 60
    assert "60 минут" in telegram.sent[-1]["text"]


def test_throttle_zero_disables(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "p5t2.db")
    try:
        telegram = FakeTelegram()
        svc = ActionRuntimeService(session, telegram_client=telegram)
        queued = svc.enqueue_action(_envelope("profile.set_throttle", minutes="0"))
        asyncio.run(svc.process_job(queued.job_id))
        profile = session.query(TenantUserProfile).one()
    finally:
        session.close()

    assert profile.proactive_throttle_minutes == 0
    assert "отключ" in telegram.sent[-1]["text"].lower()


def test_throttle_rejects_out_of_range(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "p5t3.db")
    try:
        svc = ActionRuntimeService(session, telegram_client=FakeTelegram())
        queued = svc.enqueue_action(_envelope("profile.set_throttle", minutes="9999"))
        result = asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()
    assert result == "failed"


def test_throttle_no_args_shows_current(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "p5t4.db")
    try:
        telegram = FakeTelegram()
        svc = ActionRuntimeService(session, telegram_client=telegram)
        queued = svc.enqueue_action(_envelope("profile.set_throttle", minutes=""))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    # Default 30 minutes
    assert "30 минут" in telegram.sent[-1]["text"]


# ---------------------------------------------------------------------------
# /stats handler
# ---------------------------------------------------------------------------


def test_stats_no_activity(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "p5s1.db")
    try:
        telegram = FakeTelegram()
        svc = ActionRuntimeService(session, telegram_client=telegram)
        queued = svc.enqueue_action(_envelope("stats.show"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert "тихо" in telegram.sent[-1]["text"].lower()


def test_stats_shows_sent_and_dropped(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "p5s2.db")
    try:
        # Seed outbox rows of various kinds
        now = datetime.now(timezone.utc)
        session.add_all(
            [
                OutboxMessage(
                    id="o1",
                    tenant_id="t1",
                    workspace_id="w1",
                    user_id="u1",
                    channel_type="telegram",
                    feature_key="skill_a",
                    status="sent",
                    is_interactive=False,
                    payload_json=json.dumps({"chat_id": "42", "text": "hi"}),
                    created_at=now - timedelta(hours=1),
                ),
                OutboxMessage(
                    id="o2",
                    tenant_id="t1",
                    workspace_id="w1",
                    user_id="u1",
                    channel_type="telegram",
                    feature_key="skill_a",
                    status="dropped",
                    drop_reason="duplicate",
                    is_interactive=False,
                    payload_json=json.dumps({"chat_id": "42", "text": "hi again"}),
                    created_at=now - timedelta(minutes=30),
                ),
            ]
        )
        session.commit()

        telegram = FakeTelegram()
        svc = ActionRuntimeService(session, telegram_client=telegram)
        queued = svc.enqueue_action(_envelope("stats.show"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    text = telegram.sent[-1]["text"]
    assert "skill_a" in text
    assert "отправлено: 1" in text
    assert "дубликат" in text.lower()


# ---------------------------------------------------------------------------
# End-to-end: ProactiveEventWorker applies decide_to_speak
# ---------------------------------------------------------------------------


@pytest.fixture()
def _fresh_registry(monkeypatch):
    fresh = FeatureRegistry()
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry", lambda: fresh
    )
    monkeypatch.setattr(
        "sreda.workers.proactive_events.get_feature_registry",
        lambda: fresh,
    )
    return fresh


def test_proactive_duplicate_dropped_by_policy(
    monkeypatch, tmp_path: Path, _fresh_registry
):
    """When the same handler fires twice on similar content within 24h,
    the second outbox row is dropped with reason=duplicate."""
    session = _bootstrap(monkeypatch, tmp_path, "p5e2e1.db")
    _seed_subscription(session, TEST_FEATURE_KEY)

    def handler(ctx: ProactiveEventContext):
        return [RuntimeReply(text="Заявка #42 обновлена", reply_markup=None)]

    _fresh_registry.register_proactive_handler(
        feature_key=TEST_FEATURE_KEY, handler=handler
    )

    repo = InboundEventRepository(session)
    repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-1",
            relevance_score=0.9,
        )
    )
    repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="evt-2",  # different key, same text
            relevance_score=0.9,
        )
    )
    session.commit()

    worker = ProactiveEventWorker(session)
    try:
        asyncio.run(worker.process_pending())
    finally:
        session.close()

    # Reopen a session to inspect results
    session = get_session_factory()()
    try:
        outboxes = session.query(OutboxMessage).order_by(OutboxMessage.created_at.asc()).all()
        # First → sent (pending, delivery worker hasn't run), second → dropped
        assert len(outboxes) == 2
        assert outboxes[0].status == "pending"
        assert outboxes[0].drop_reason is None
        assert outboxes[1].status == "dropped"
        assert outboxes[1].drop_reason == "duplicate"
    finally:
        session.close()


def test_proactive_throttle_defers_second_event(
    monkeypatch, tmp_path: Path, _fresh_registry
):
    """With explicit throttle=30 in profile, a second proactive reply
    (unique text) within the window is deferred rather than dropped."""
    session = _bootstrap(monkeypatch, tmp_path, "p5e2e2.db")
    _seed_subscription(session, TEST_FEATURE_KEY)

    # Default throttle is 0 (disabled). Explicitly set throttle=30 in
    # user profile to test the deferral behaviour.
    session.add(
        TenantUserProfile(
            id="prof1",
            tenant_id="t1",
            user_id="u1",
            proactive_throttle_minutes=30,
        )
    )
    session.commit()

    counter = [0]

    def handler(ctx: ProactiveEventContext):
        counter[0] += 1
        return [
            RuntimeReply(text=f"Event number {counter[0]}", reply_markup=None)
        ]

    _fresh_registry.register_proactive_handler(
        feature_key=TEST_FEATURE_KEY, handler=handler
    )

    repo = InboundEventRepository(session)
    repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="e1",
            relevance_score=0.9,
        )
    )
    repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=TEST_FEATURE_KEY,
            event_type="synthetic",
            external_event_key="e2",
            relevance_score=0.9,
        )
    )
    session.commit()

    worker = ProactiveEventWorker(session)
    try:
        asyncio.run(worker.process_pending())
    finally:
        session.close()

    session = get_session_factory()()
    try:
        outboxes = session.query(OutboxMessage).order_by(OutboxMessage.created_at.asc()).all()
        assert len(outboxes) == 2
        # First message → pending (first event of the window)
        assert outboxes[0].scheduled_at is None
        # Second → deferred
        assert outboxes[1].status == "pending"
        assert outboxes[1].scheduled_at is not None
    finally:
        session.close()
