"""Phase 2a sanity tests — profile + skill-config repo CRUD + validation."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.repositories.user_profile import UserProfileRepository


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Tenant 1"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def test_get_or_create_profile_is_idempotent(session):
    repo = UserProfileRepository(session)
    first = repo.get_or_create_profile("t1", "u1")
    second = repo.get_or_create_profile("t1", "u1")
    assert first.id == second.id
    assert first.timezone == "UTC"
    assert first.communication_style == "casual"


def test_update_profile_persists_quiet_hours_and_style(session):
    repo = UserProfileRepository(session)
    profile = repo.update_profile(
        "t1",
        "u1",
        source="user_command",
        actor_user_id="u1",
        tz="Europe/Moscow",
        quiet_hours=[{"from_hour": 22, "to_hour": 8, "weekdays": [0, 1, 2, 3, 4]}],
        communication_style="terse",
        interest_tags=["работа", "спорт"],
    )
    session.commit()

    refreshed = repo.get_profile("t1", "u1")
    assert refreshed is not None
    assert refreshed.timezone == "Europe/Moscow"
    assert refreshed.communication_style == "terse"
    assert refreshed.updated_by_source == "user_command"

    decoded_windows = UserProfileRepository.decode_quiet_hours(refreshed)
    assert decoded_windows == [
        {"from_hour": 22, "to_hour": 8, "weekdays": [0, 1, 2, 3, 4]}
    ]
    assert UserProfileRepository.decode_interest_tags(refreshed) == ["работа", "спорт"]


def test_update_profile_rejects_bad_style(session):
    repo = UserProfileRepository(session)
    with pytest.raises(ValueError):
        repo.update_profile("t1", "u1", communication_style="weird")


def test_update_profile_rejects_bad_source(session):
    repo = UserProfileRepository(session)
    with pytest.raises(ValueError):
        repo.update_profile("t1", "u1", source="hacker")


def test_update_profile_rejects_bad_quiet_hours(session):
    repo = UserProfileRepository(session)
    with pytest.raises(ValueError):
        repo.update_profile(
            "t1",
            "u1",
            quiet_hours=[{"from_hour": 25, "to_hour": 8}],
        )


def test_upsert_skill_config_creates_and_updates(session):
    repo = UserProfileRepository(session)
    row1 = repo.upsert_skill_config(
        "t1",
        "u1",
        "eds_monitor",
        notification_priority="urgent",
        token_budget_daily=5000,
        skill_params={"alert_keywords": ["pizza"]},
    )
    session.commit()

    assert row1.notification_priority == "urgent"
    assert row1.token_budget_daily == 5000
    assert UserProfileRepository.decode_skill_params(row1) == {
        "alert_keywords": ["pizza"]
    }

    row2 = repo.upsert_skill_config(
        "t1", "u1", "eds_monitor", notification_priority="mute"
    )
    session.commit()

    assert row2.id == row1.id
    assert row2.notification_priority == "mute"
    # unchanged fields stay put
    assert row2.token_budget_daily == 5000


def test_upsert_skill_config_rejects_unknown_priority(session):
    repo = UserProfileRepository(session)
    with pytest.raises(ValueError):
        repo.upsert_skill_config(
            "t1", "u1", "eds_monitor", notification_priority="whenever"
        )


def test_list_skill_configs_scoped_by_user(session):
    repo = UserProfileRepository(session)
    repo.upsert_skill_config("t1", "u1", "eds_monitor")
    repo.upsert_skill_config("t1", "u1", "stub_skill")
    session.commit()

    configs = repo.list_skill_configs("t1", "u1")
    keys = sorted(c.feature_key for c in configs)
    assert keys == ["eds_monitor", "stub_skill"]
