"""Phase 2e: hybrid-UX confirm flow.

Covers propose → confirm and propose → reject paths end-to-end, plus
expiry and ownership checks. The propose action is invoked directly as
a stand-in for the future LLM-agent tool call.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    Assistant,
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
        source_type="system",
        source_value=action_type,
        params=params,
    )


def test_dispatcher_parses_confirm_callback():
    assert _resolve_callback_action("profile:confirm:tupp_abc123") == (
        "profile.confirm_update",
        {"proposal_id": "tupp_abc123"},
    )
    assert _resolve_callback_action("profile:reject:tupp_abc123") == (
        "profile.reject_update",
        {"proposal_id": "tupp_abc123"},
    )


def test_propose_creates_row_and_sends_message_with_buttons(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "pr1.db")
    try:
        telegram = FakeTelegram()
        service = ActionRuntimeService(session, telegram_client=telegram)
        queued = service.enqueue_action(
            _envelope(
                "profile.propose_update",
                field_name="communication_style",
                proposed_value="terse",
                justification="По истории ты пишешь коротко.",
            )
        )
        asyncio.run(service.process_job(queued.job_id))

        proposals = session.query(TenantUserProfileProposal).all()
        assert len(proposals) == 1
        assert proposals[0].status == "pending"
        assert proposals[0].field_name == "communication_style"
    finally:
        session.close()

    msg = telegram.sent[0]
    assert "Предлагаю обновить" in msg["text"]
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"].startswith("profile:confirm:")
    assert buttons[1]["callback_data"].startswith("profile:reject:")


def test_propose_rejects_invalid_field(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "pr2.db")
    try:
        telegram = FakeTelegram()
        service = ActionRuntimeService(session, telegram_client=telegram)
        queued = service.enqueue_action(
            _envelope(
                "profile.propose_update",
                field_name="evil_field",
                proposed_value="yolo",
            )
        )
        result = asyncio.run(service.process_job(queued.job_id))
        assert result == "failed"
        assert session.query(TenantUserProfileProposal).count() == 0
    finally:
        session.close()


def test_propose_rejects_invalid_timezone(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "pr3.db")
    try:
        telegram = FakeTelegram()
        service = ActionRuntimeService(session, telegram_client=telegram)
        queued = service.enqueue_action(
            _envelope(
                "profile.propose_update",
                field_name="timezone",
                proposed_value="Mars/Olympus",
            )
        )
        result = asyncio.run(service.process_job(queued.job_id))
        assert result == "failed"
    finally:
        session.close()


def test_confirm_applies_change_with_audit_source(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "pr4.db")
    try:
        telegram = FakeTelegram()
        service = ActionRuntimeService(session, telegram_client=telegram)

        # Propose
        queued = service.enqueue_action(
            _envelope(
                "profile.propose_update",
                field_name="communication_style",
                proposed_value="terse",
            )
        )
        asyncio.run(service.process_job(queued.job_id))
        proposal = session.query(TenantUserProfileProposal).one()

        # Confirm
        queued_confirm = service.enqueue_action(
            _envelope("profile.confirm_update", proposal_id=proposal.id)
        )
        asyncio.run(service.process_job(queued_confirm.job_id))

        session.refresh(proposal)
        profile = session.query(TenantUserProfile).one()
    finally:
        session.close()

    assert proposal.status == "confirmed"
    assert proposal.completed_at is not None
    assert profile.communication_style == "terse"
    assert profile.updated_by_source == "agent_tool_confirmed"
    assert profile.updated_by_user_id == "u1"

    # Two messages: proposal + confirmation reply
    assert len(telegram.sent) == 2
    assert "обновлён" in telegram.sent[1]["text"]


def test_reject_does_not_change_profile(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "pr5.db")
    try:
        telegram = FakeTelegram()
        service = ActionRuntimeService(session, telegram_client=telegram)

        queued = service.enqueue_action(
            _envelope(
                "profile.propose_update",
                field_name="communication_style",
                proposed_value="formal",
            )
        )
        asyncio.run(service.process_job(queued.job_id))
        proposal = session.query(TenantUserProfileProposal).one()

        queued_reject = service.enqueue_action(
            _envelope("profile.reject_update", proposal_id=proposal.id)
        )
        asyncio.run(service.process_job(queued_reject.job_id))

        session.refresh(proposal)
        profile = session.query(TenantUserProfile).one_or_none()
    finally:
        session.close()

    assert proposal.status == "rejected"
    # Profile either wasn't created or style remains default
    assert profile is None or profile.communication_style == "casual"


def test_expired_proposal_cannot_be_confirmed(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "pr6.db")
    try:
        telegram = FakeTelegram()
        service = ActionRuntimeService(session, telegram_client=telegram)

        # Create proposal with expired TTL directly via repo
        repo = UserProfileRepository(session)
        proposal = repo.create_proposal(
            "t1",
            "u1",
            field_name="communication_style",
            proposed_value="terse",
            ttl=timedelta(seconds=-1),
        )
        session.commit()

        queued = service.enqueue_action(
            _envelope("profile.confirm_update", proposal_id=proposal.id)
        )
        result = asyncio.run(service.process_job(queued.job_id))
        session.refresh(proposal)
    finally:
        session.close()

    assert result == "failed"
    assert proposal.status == "expired"


def test_cannot_confirm_other_users_proposal(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "pr7.db")
    try:
        # Add a second user u2 in same tenant
        session.add(User(id="u2", tenant_id="t1", telegram_account_id="99"))
        session.commit()

        telegram = FakeTelegram()
        service = ActionRuntimeService(session, telegram_client=telegram)

        # Proposal belongs to u1
        repo = UserProfileRepository(session)
        proposal = repo.create_proposal(
            "t1",
            "u1",
            field_name="communication_style",
            proposed_value="terse",
        )
        session.commit()

        # u2 tries to confirm — action carries user_id=u2
        envelope_u2 = ActionEnvelope(
            action_type="profile.confirm_update",
            tenant_id="t1",
            workspace_id="w1",
            assistant_id="a1",
            user_id="u2",
            channel_type="telegram_dm",
            external_chat_id="99",
            bot_key="sreda",
            inbound_message_id=None,
            source_type="telegram_callback",
            source_value="profile:confirm:foo",
            params={"proposal_id": proposal.id},
        )
        queued = service.enqueue_action(envelope_u2)
        result = asyncio.run(service.process_job(queued.job_id))
        session.refresh(proposal)
    finally:
        session.close()

    assert result == "failed"
    # Proposal remains pending
    assert proposal.status == "pending"
