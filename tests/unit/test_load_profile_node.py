"""Phase 2c: verify the ``load_profile`` graph node reads profile +
skill configs into state, or returns empty when the user has no row."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User, Workspace
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.graph import node_load_profile


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Tenant 1"))
    sess.add(Workspace(id="w1", tenant_id="t1", name="Workspace 1"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def _state_for(user_id: str | None) -> dict:
    return {
        "action": ActionEnvelope(
            action_type="help.show",
            tenant_id="t1",
            workspace_id="w1",
            assistant_id=None,
            user_id=user_id,
            channel_type="telegram_dm",
            external_chat_id="42",
            bot_key="sreda",
            inbound_message_id=None,
            source_type="telegram_message",
            source_value="/help",
            params={},
        ).as_dict()
    }


def test_load_profile_returns_empty_when_user_missing(session):
    config = {"configurable": {"session": session}}
    result = node_load_profile(_state_for(None), config)
    assert result == {"profile": {}, "skill_configs": []}


def test_load_profile_returns_empty_when_profile_not_created(session):
    config = {"configurable": {"session": session}}
    result = node_load_profile(_state_for("u1"), config)
    assert result["profile"] == {}
    assert result["skill_configs"] == []


def test_load_profile_reads_existing_profile(session):
    repo = UserProfileRepository(session)
    repo.update_profile(
        "t1",
        "u1",
        tz="Europe/Moscow",
        quiet_hours=[{"from_hour": 22, "to_hour": 8, "weekdays": [0, 1, 2, 3, 4]}],
        communication_style="terse",
        interest_tags=["работа"],
    )
    repo.upsert_skill_config(
        "t1",
        "u1",
        "eds_monitor",
        notification_priority="urgent",
        token_budget_daily=5000,
    )
    session.commit()

    config = {"configurable": {"session": session}}
    result = node_load_profile(_state_for("u1"), config)
    assert result["profile"]["timezone"] == "Europe/Moscow"
    assert result["profile"]["communication_style"] == "terse"
    assert result["profile"]["quiet_hours"] == [
        {"from_hour": 22, "to_hour": 8, "weekdays": [0, 1, 2, 3, 4]}
    ]
    assert result["profile"]["interest_tags"] == ["работа"]

    assert len(result["skill_configs"]) == 1
    cfg = result["skill_configs"][0]
    assert cfg["feature_key"] == "eds_monitor"
    assert cfg["notification_priority"] == "urgent"
    assert cfg["token_budget_daily"] == 5000
