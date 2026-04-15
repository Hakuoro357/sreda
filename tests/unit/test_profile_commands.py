"""Phase 2b integration: profile + skill commands through the graph.

Covers the user-visible command surface:
  * ``/profile``                            → profile.show
  * ``/quiet 22-8``, ``/quiet off``         → profile.set_quiet_hours
  * ``/skills``                             → skills.list
  * ``/skill <key>``                        → skill.show
  * ``/skill <key> priority <level>``       → skill.set_priority

Each test drives the full runtime (enqueue_action → process_job → graph)
so we exercise the dispatcher, handler, and DB write paths end-to-end.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    Assistant,
    Tenant,
    TenantUserProfile,
    TenantUserSkillConfig,
    User,
    Workspace,
)
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.db.session import get_engine, get_session_factory
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.executor import ActionRuntimeService


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True}


def _bootstrap(monkeypatch, tmp_path: Path, db_name: str):
    db_path = tmp_path / db_name
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    session.add(Tenant(id="t1", name="Tenant 1"))
    session.add(Workspace(id="w1", tenant_id="t1", name="Workspace 1"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id="t1", workspace_id="w1", name="Sreda"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="100000003"))
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
        external_chat_id="100000003",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_message",
        source_value=f"/{action_type}",
        params=params,
    )


def test_profile_show_creates_profile_and_renders(monkeypatch, tmp_path: Path) -> None:
    session = _bootstrap(monkeypatch, tmp_path, "p1.db")
    try:
        telegram = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram)
        queued = service.enqueue_action(_envelope("profile.show"))
        asyncio.run(service.process_job(queued.job_id))

        profile = session.query(TenantUserProfile).one()
        assert profile.timezone == "UTC"
        assert profile.communication_style == "casual"
    finally:
        session.close()

    assert len(telegram.sent_messages) == 1
    text = telegram.sent_messages[0]["text"]
    assert "Профиль" in text
    assert "UTC" in text
    assert "casual" in text


def test_quiet_hours_set_and_clear(monkeypatch, tmp_path: Path) -> None:
    session = _bootstrap(monkeypatch, tmp_path, "p2.db")
    try:
        telegram = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram)

        # Set
        queued_set = service.enqueue_action(
            _envelope("profile.set_quiet_hours", args_raw="22-8")
        )
        asyncio.run(service.process_job(queued_set.job_id))

        profile = session.query(TenantUserProfile).one()
        windows = UserProfileRepository.decode_quiet_hours(profile)
        assert windows == [
            {"from_hour": 22, "to_hour": 8, "weekdays": [0, 1, 2, 3, 4, 5, 6]}
        ]
        assert profile.updated_by_source == "user_command"
        assert profile.updated_by_user_id == "u1"

        # Clear
        queued_clear = service.enqueue_action(
            _envelope("profile.set_quiet_hours", args_raw="off")
        )
        asyncio.run(service.process_job(queued_clear.job_id))
        session.refresh(profile)
        assert UserProfileRepository.decode_quiet_hours(profile) == []
    finally:
        session.close()

    # Two confirmation messages
    assert len(telegram.sent_messages) == 2
    assert "22:00" in telegram.sent_messages[0]["text"]
    assert "сняты" in telegram.sent_messages[1]["text"]


def test_quiet_hours_rejects_garbage(monkeypatch, tmp_path: Path) -> None:
    session = _bootstrap(monkeypatch, tmp_path, "p3.db")
    try:
        telegram = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram)
        queued = service.enqueue_action(
            _envelope("profile.set_quiet_hours", args_raw="wat-ever")
        )
        result = asyncio.run(service.process_job(queued.job_id))
        assert result == "failed"
        # No profile row should have been written
        assert session.query(TenantUserProfile).count() == 0
    finally:
        session.close()

    assert len(telegram.sent_messages) == 1
    assert "Используй" in telegram.sent_messages[0]["text"]


def test_skill_set_priority_writes_row(monkeypatch, tmp_path: Path) -> None:
    session = _bootstrap(monkeypatch, tmp_path, "p4.db")
    try:
        telegram = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram)
        # stub_skill is auto-loaded when the test environment imports it;
        # we bootstrap it explicitly so the registry has at least one manifest.
        from sreda.features.app_registry import get_feature_registry
        from sreda.features.stub_skill import STUB_SKILL_FEATURE_KEY, register as _register_stub

        registry = get_feature_registry()
        if registry.get_manifest(STUB_SKILL_FEATURE_KEY) is None:
            _register_stub(registry)

        queued = service.enqueue_action(
            _envelope(
                "skill.set_priority",
                feature_key=STUB_SKILL_FEATURE_KEY,
                priority="mute",
            )
        )
        asyncio.run(service.process_job(queued.job_id))

        rows = session.query(TenantUserSkillConfig).all()
        assert len(rows) == 1
        assert rows[0].feature_key == STUB_SKILL_FEATURE_KEY
        assert rows[0].notification_priority == "mute"
        assert rows[0].updated_by_source == "user_command"
    finally:
        session.close()

    assert len(telegram.sent_messages) == 1
    assert "mute" in telegram.sent_messages[0]["text"]


def test_skill_set_priority_rejects_unknown_skill(monkeypatch, tmp_path: Path) -> None:
    session = _bootstrap(monkeypatch, tmp_path, "p5.db")
    try:
        telegram = FakeTelegramClient()
        service = ActionRuntimeService(session, telegram_client=telegram)
        queued = service.enqueue_action(
            _envelope(
                "skill.set_priority",
                feature_key="no_such_skill",
                priority="urgent",
            )
        )
        result = asyncio.run(service.process_job(queued.job_id))
        assert result == "failed"
        assert session.query(TenantUserSkillConfig).count() == 0
    finally:
        session.close()

    assert len(telegram.sent_messages) == 1
    assert "не найден" in telegram.sent_messages[0]["text"]


def test_dispatcher_parses_quiet_command() -> None:
    from sreda.runtime.dispatcher import _resolve_command_action

    assert _resolve_command_action("/quiet 22-8") == (
        "profile.set_quiet_hours",
        {"args_raw": "22-8"},
    )
    assert _resolve_command_action("/quiet off") == (
        "profile.set_quiet_hours",
        {"args_raw": "off"},
    )
    # bare /quiet with no args routes to show
    assert _resolve_command_action("/quiet") == ("profile.show", {})


def test_dispatcher_parses_skill_command() -> None:
    from sreda.runtime.dispatcher import _resolve_command_action

    assert _resolve_command_action("/skill eds_monitor") == (
        "skill.show",
        {"feature_key": "eds_monitor"},
    )
    assert _resolve_command_action("/skill eds_monitor priority mute") == (
        "skill.set_priority",
        {"feature_key": "eds_monitor", "priority": "mute"},
    )
    assert _resolve_command_action("/skills") == ("skills.list", {})
