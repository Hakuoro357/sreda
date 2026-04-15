"""Phase 2 acceptance tests (plan §Фаза 2).

Three integration tests that map 1:1 to the roadmap's acceptance
criteria. These drive the full runtime (graph + delivery worker +
dispatcher + DB) with controlled time to prove the defer/mute/urgent
cycles work end-to-end.

1. quiet_hours: proactive EDS notification at 23:00 MSK → deferred
   to 08:00 MSK; at wake-time the worker sends it.
2. hybrid-UX: agent proposes communication_style change → inline
   confirm button → only on user confirm the profile changes, with
   audit source ``agent_tool_confirmed``.
3. per-skill: ``eds_monitor.priority = mute`` silences proactive EDS
   events; ``priority = urgent`` delivers them even inside quiet hours.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    Assistant,
    OutboxMessage,
    Tenant,
    TenantUserProfile,
    TenantUserProfileProposal,
    User,
    Workspace,
)
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.db.session import get_engine, get_session_factory
from sreda.runtime.dispatcher import ActionEnvelope, _resolve_callback_action
from sreda.runtime.executor import ActionRuntimeService
from sreda.workers.outbox_delivery import OutboxDeliveryWorker


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id: str, text: str, reply_markup=None, **kwargs):
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
        source_type="telegram_callback",
        source_value=action_type,
        params=params,
    )


def _inject_proactive_eds_outbox(
    session, *, text: str = "Новое событие по заявке #123"
) -> OutboxMessage:
    """Simulate what the EDS monitor feature will eventually do: push a
    notification straight into the outbox (no graph, no user command)."""
    row = OutboxMessage(
        id=f"out_{uuid4().hex[:16]}",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        channel_type="telegram",
        feature_key="eds_monitor",
        is_interactive=False,
        status="pending",
        payload_json=json.dumps(
            {"chat_id": "42", "text": text, "reply_markup": None},
            ensure_ascii=False,
        ),
    )
    session.add(row)
    session.commit()
    return row


def _utc(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# =========================================================================
# 1. Quiet hours: defer EDS notification at 23:00 MSK → wake at 08:00 MSK
# =========================================================================


def test_acceptance_quiet_hours_defers_and_wakes(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "acc1.db")
    try:
        telegram = FakeTelegram()
        # User sets TZ + quiet hours through the runtime (production path)
        service = ActionRuntimeService(session, telegram_client=telegram)
        asyncio.run(
            service.process_job(
                service.enqueue_action(
                    _envelope("profile.set_timezone", timezone="Europe/Moscow")
                ).job_id
            )
        )
        asyncio.run(
            service.process_job(
                service.enqueue_action(
                    _envelope("profile.set_quiet_hours", args_raw="22-8")
                ).job_id
            )
        )

        profile = session.query(TenantUserProfile).one()
        assert profile.timezone == "Europe/Moscow"
        windows = UserProfileRepository.decode_quiet_hours(profile)
        assert windows == [
            {"from_hour": 22, "to_hour": 8, "weekdays": [0, 1, 2, 3, 4, 5, 6]}
        ]

        # EDS fires a proactive event at 23:00 MSK == 20:00 UTC
        eds_row = _inject_proactive_eds_outbox(session)
        sent_before = len(telegram.sent)

        worker = OutboxDeliveryWorker(session, telegram_client=telegram)
        now_2300 = _utc(2026, 4, 15, 20, 0)
        processed = asyncio.run(worker.process_pending_messages(now=now_2300))
        assert processed == 1

        session.refresh(eds_row)
        assert eds_row.status == "pending"
        # Telegram was not called — quiet hours held the message back
        assert len(telegram.sent) == sent_before
        # scheduled_at is at 08:00 MSK next day == 05:00 UTC
        scheduled = eds_row.scheduled_at
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        assert scheduled == _utc(2026, 4, 16, 5, 0)

        # Advance time: still inside quiet (04:00 UTC == 07:00 MSK)
        asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 16, 4, 0)))
        session.refresh(eds_row)
        assert eds_row.status == "pending"
        assert len(telegram.sent) == sent_before

        # Now after 08:00 MSK (06:00 UTC) — worker picks it up
        asyncio.run(worker.process_pending_messages(now=_utc(2026, 4, 16, 6, 0)))
        session.refresh(eds_row)
    finally:
        session.close()

    assert eds_row.status == "sent"
    # Last message in the tape is the EDS notification
    assert any(
        msg["text"].startswith("Новое событие") for msg in telegram.sent
    ), telegram.sent


# =========================================================================
# 2. Hybrid-UX: agent proposes style → inline confirm → profile updated
# =========================================================================


def test_acceptance_hybrid_ux_confirm_flow(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "acc2.db")
    try:
        telegram = FakeTelegram()
        service = ActionRuntimeService(session, telegram_client=telegram)

        # --- Agent proposes communication_style = "terse"
        propose = service.enqueue_action(
            _envelope(
                "profile.propose_update",
                field_name="communication_style",
                proposed_value="terse",
                justification="Замечаю, что ты обычно пишешь коротко.",
            )
        )
        asyncio.run(service.process_job(propose.job_id))

        proposal = session.query(TenantUserProfileProposal).one()
        assert proposal.status == "pending"
        # Profile not created yet OR defaults preserved
        existing = session.query(TenantUserProfile).one_or_none()
        assert existing is None or existing.communication_style == "casual"

        # Telegram got the confirm prompt with inline buttons
        prompt = telegram.sent[0]
        confirm_button = prompt["reply_markup"]["inline_keyboard"][0][0]
        assert confirm_button["callback_data"] == f"profile:confirm:{proposal.id}"

        # --- User clicks "Подтвердить" → webhook sends callback_data
        # The dispatcher translates that to an action envelope.
        resolved = _resolve_callback_action(confirm_button["callback_data"])
        assert resolved == (
            "profile.confirm_update",
            {"proposal_id": proposal.id},
        )
        action_type, params = resolved
        confirm_envelope = ActionEnvelope(
            action_type=action_type,
            tenant_id="t1",
            workspace_id="w1",
            assistant_id="a1",
            user_id="u1",
            channel_type="telegram_dm",
            external_chat_id="42",
            bot_key="sreda",
            inbound_message_id=None,  # callback, not a new inbound
            source_type="telegram_callback",
            source_value=confirm_button["callback_data"],
            params=params,
        )
        queued_confirm = service.enqueue_action(confirm_envelope)
        asyncio.run(service.process_job(queued_confirm.job_id))

        session.refresh(proposal)
        profile = session.query(TenantUserProfile).one()
    finally:
        session.close()

    # Proposal is closed, profile is updated, audit source is the
    # agent_tool_confirmed value (differentiating it from a direct
    # ``user_command`` path).
    assert proposal.status == "confirmed"
    assert profile.communication_style == "terse"
    assert profile.updated_by_source == "agent_tool_confirmed"


# =========================================================================
# 3. Per-skill priority: mute silences, urgent bypasses quiet hours
# =========================================================================


def test_acceptance_per_skill_mute_and_urgent(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "acc3.db")
    try:
        telegram = FakeTelegram()
        service = ActionRuntimeService(session, telegram_client=telegram)

        # Configure user: MSK + quiet hours 22-8 so we can test
        # "urgent bypasses quiet" later.
        asyncio.run(
            service.process_job(
                service.enqueue_action(
                    _envelope("profile.set_timezone", timezone="Europe/Moscow")
                ).job_id
            )
        )
        asyncio.run(
            service.process_job(
                service.enqueue_action(
                    _envelope("profile.set_quiet_hours", args_raw="22-8")
                ).job_id
            )
        )
        # Register stub_skill so the registry has the key (acceptance
        # tests don't care which skill — we just need some feature_key).
        from sreda.features.app_registry import get_feature_registry
        from sreda.features.stub_skill import (
            STUB_SKILL_FEATURE_KEY,
            register as _register_stub,
        )

        registry = get_feature_registry()
        if registry.get_manifest(STUB_SKILL_FEATURE_KEY) is None:
            _register_stub(registry)

        # --- A. Mute eds_monitor → proactive EDS event is DROPPED.
        # We write the config directly via the repo: the ``/skill <key>
        # priority <level>`` command validates that the feature_key is
        # in the registry, and ``eds_monitor``'s manifest is registered
        # only when the private package is loaded via
        # ``SREDA_FEATURE_MODULES`` (which tests don't do). The delivery
        # worker itself only reads ``tenant_user_skill_configs`` — no
        # registry lookup — so bypassing the command handler is safe.
        repo = UserProfileRepository(session)
        repo.upsert_skill_config(
            "t1", "u1", "eds_monitor", notification_priority="mute"
        )
        session.commit()

        muted_row = _inject_proactive_eds_outbox(session, text="EDS мут событие")
        sent_before_mute = len(telegram.sent)

        worker = OutboxDeliveryWorker(session, telegram_client=telegram)
        # 23:00 MSK
        asyncio.run(
            worker.process_pending_messages(now=_utc(2026, 4, 15, 20, 0))
        )
        session.refresh(muted_row)
        assert muted_row.status == "muted"
        # Telegram not called for this row
        assert not any(
            msg.get("text") == "EDS мут событие" for msg in telegram.sent[sent_before_mute:]
        )

        # --- B. Urgent eds_monitor → sent DESPITE quiet hours
        repo.upsert_skill_config(
            "t1", "u1", "eds_monitor", notification_priority="urgent"
        )
        session.commit()

        urgent_row = _inject_proactive_eds_outbox(session, text="EDS срочное событие")

        # Same 23:00 MSK — user is in quiet hours
        asyncio.run(
            worker.process_pending_messages(now=_utc(2026, 4, 15, 20, 0))
        )
        session.refresh(urgent_row)
    finally:
        session.close()

    assert urgent_row.status == "sent"
    assert any(
        msg.get("text") == "EDS срочное событие" for msg in telegram.sent
    ), telegram.sent
