"""Unit tests for HousewifeOnboardingService — onboarding state machine."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.services.housewife_onboarding import (
    HOUSEWIFE_FEATURE_KEY,
    STATE_ANSWERED,
    STATE_PENDING,
    STATE_SKIPPED,
    STATE_SKIPPED_ONCE,
    STATUS_COMPLETE,
    STATUS_IN_PROGRESS,
    STATUS_NOT_STARTED,
    TOPIC_ADDRESSING,
    TOPIC_DIET,
    TOPIC_FAMILY,
    TOPIC_ORDER,
    TOPIC_PAIN_POINT,
    TOPIC_ROUTINE,
    TOPIC_SELF_INTRO,
    HousewifeOnboardingService,
    _next_topic,
)


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="t1", name="Test"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    session.commit()
    return session


# ---------------------------------------------------------------------------
# next_topic picker
# ---------------------------------------------------------------------------


def test_next_topic_first_pending_wins():
    state = {
        "topics": {
            TOPIC_ADDRESSING: {"state": STATE_ANSWERED},
            TOPIC_SELF_INTRO: {"state": STATE_ANSWERED},
            TOPIC_FAMILY: {"state": STATE_PENDING},
            TOPIC_DIET: {"state": STATE_PENDING},
            TOPIC_ROUTINE: {"state": STATE_PENDING},
            TOPIC_PAIN_POINT: {"state": STATE_PENDING},
        }
    }
    assert _next_topic(state["topics"]) == TOPIC_FAMILY


def test_next_topic_skipped_once_gets_second_pass():
    """After all pending exhausted, skipped_once topics come back."""
    state = {
        "topics": {
            TOPIC_ADDRESSING: {"state": STATE_ANSWERED},
            TOPIC_SELF_INTRO: {"state": STATE_SKIPPED_ONCE},
            TOPIC_FAMILY: {"state": STATE_ANSWERED},
            TOPIC_DIET: {"state": STATE_ANSWERED},
            TOPIC_ROUTINE: {"state": STATE_ANSWERED},
            TOPIC_PAIN_POINT: {"state": STATE_ANSWERED},
        }
    }
    assert _next_topic(state["topics"]) == TOPIC_SELF_INTRO


def test_next_topic_returns_none_when_all_settled():
    state = {
        "topics": {
            key: {"state": STATE_ANSWERED if i % 2 == 0 else STATE_SKIPPED}
            for i, key in enumerate(TOPIC_ORDER)
        }
    }
    assert _next_topic(state["topics"]) is None


# ---------------------------------------------------------------------------
# Service: read / initialize / start
# ---------------------------------------------------------------------------


def test_get_raw_state_returns_default_when_no_config():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    assert state["status"] == STATUS_NOT_STARTED
    assert state["current_topic"] is None
    assert set(state["topics"].keys()) == set(TOPIC_ORDER)


def test_initialize_creates_fresh_state():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    state = service.initialize(tenant_id="t1", user_id="u1")
    assert state["status"] == STATUS_NOT_STARTED
    assert all(
        state["topics"][k]["state"] == STATE_PENDING for k in TOPIC_ORDER
    )


def test_initialize_is_idempotent_if_already_in_progress():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.initialize(tenant_id="t1", user_id="u1")
    started = service.start(tenant_id="t1", user_id="u1")
    assert started["status"] == STATUS_IN_PROGRESS

    # Re-initialize — should not reset.
    reinit = service.initialize(tenant_id="t1", user_id="u1")
    assert reinit["status"] == STATUS_IN_PROGRESS


def test_start_transitions_to_in_progress_and_picks_first_topic():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.initialize(tenant_id="t1", user_id="u1")

    state = service.start(tenant_id="t1", user_id="u1")

    assert state["status"] == STATUS_IN_PROGRESS
    assert state["current_topic"] == TOPIC_ADDRESSING
    assert state["started_at"] is not None
    assert state["current_topic_depth"] == 0


# ---------------------------------------------------------------------------
# Service: mark_answered progression
# ---------------------------------------------------------------------------


def test_mark_answered_advances_topic_and_saves_summary():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    state = service.mark_answered(
        tenant_id="t1",
        user_id="u1",
        topic=TOPIC_ADDRESSING,
        summary="Борис",
    )

    assert state["topics"][TOPIC_ADDRESSING]["state"] == STATE_ANSWERED
    assert state["topics"][TOPIC_ADDRESSING]["answer"] == "Борис"
    assert state["current_topic"] == TOPIC_SELF_INTRO
    assert state["current_topic_depth"] == 0


def test_mark_answered_addressing_mirrors_into_profile():
    """addressing → display_name should be pushed to TenantUserProfile."""
    from sreda.db.repositories.user_profile import UserProfileRepository

    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    service.mark_answered(
        tenant_id="t1",
        user_id="u1",
        topic=TOPIC_ADDRESSING,
        summary="Борис Петрович",
    )

    profile = UserProfileRepository(session).get_profile("t1", "u1")
    assert profile is not None
    assert profile.display_name == "Борис Петрович"


def test_mark_answered_all_topics_flips_status_to_complete():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    for topic in TOPIC_ORDER:
        service.mark_answered(
            tenant_id="t1",
            user_id="u1",
            topic=topic,
            summary=f"answer for {topic}",
        )

    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    assert state["status"] == STATUS_COMPLETE
    assert state["current_topic"] is None
    assert state["completed_at"] is not None


# ---------------------------------------------------------------------------
# Service: mark_deferred two-pass skip
# ---------------------------------------------------------------------------


def test_mark_deferred_once_moves_to_skipped_once_and_retries_later():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    service.mark_deferred(
        tenant_id="t1", user_id="u1", topic=TOPIC_ADDRESSING
    )
    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    assert state["topics"][TOPIC_ADDRESSING]["state"] == STATE_SKIPPED_ONCE
    # Next topic should be the next pending one.
    assert state["current_topic"] == TOPIC_SELF_INTRO

    # Answer the rest except addressing; after that, addressing should
    # come back up for a second chance.
    for topic in (
        TOPIC_SELF_INTRO,
        TOPIC_FAMILY,
        TOPIC_DIET,
        TOPIC_ROUTINE,
        TOPIC_PAIN_POINT,
    ):
        service.mark_answered(
            tenant_id="t1", user_id="u1", topic=topic, summary="ok"
        )

    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    # Five answered, addressing still skipped_once → that's the current.
    assert state["current_topic"] == TOPIC_ADDRESSING


def test_mark_deferred_twice_becomes_permanent_skip():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    service.mark_deferred(
        tenant_id="t1", user_id="u1", topic=TOPIC_ADDRESSING
    )
    service.mark_deferred(
        tenant_id="t1", user_id="u1", topic=TOPIC_ADDRESSING
    )
    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    assert state["topics"][TOPIC_ADDRESSING]["state"] == STATE_SKIPPED


def test_all_topics_skipped_flips_to_complete():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    for topic in TOPIC_ORDER:
        service.mark_deferred(tenant_id="t1", user_id="u1", topic=topic)
    # Second pass: skipped_once → skipped
    for topic in TOPIC_ORDER:
        service.mark_deferred(tenant_id="t1", user_id="u1", topic=topic)

    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    assert state["status"] == STATUS_COMPLETE


# ---------------------------------------------------------------------------
# Service: follow-up depth
# ---------------------------------------------------------------------------


def test_record_follow_up_increments_up_to_cap():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    for expected in (1, 2, 2, 2):  # caps at 2
        state = service.record_follow_up(tenant_id="t1", user_id="u1")
        assert state["current_topic_depth"] == expected


def test_record_follow_up_noop_when_not_in_progress():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    # never call start() — status stays not_started

    state = service.record_follow_up(tenant_id="t1", user_id="u1")
    assert state["current_topic_depth"] == 0
    assert state["status"] == STATUS_NOT_STARTED


# ---------------------------------------------------------------------------
# Service: prompt rendering
# ---------------------------------------------------------------------------


def test_format_for_prompt_shows_current_topic_and_status_markers():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")
    service.mark_answered(
        tenant_id="t1", user_id="u1",
        topic=TOPIC_ADDRESSING, summary="Борис",
    )

    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    prompt = service.format_for_prompt(state)

    # Current topic block
    assert TOPIC_SELF_INTRO in prompt
    # Marker for answered
    assert "✅" in prompt
    assert "Борис" in prompt  # saved answer shown
    # Tool call hints for the LLM
    assert "onboarding_answered" in prompt
    assert "onboarding_deferred" in prompt


def test_format_for_prompt_depth_warning_at_cap():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")
    service.record_follow_up(tenant_id="t1", user_id="u1")
    service.record_follow_up(tenant_id="t1", user_id="u1")

    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    prompt = service.format_for_prompt(state)

    assert "БОЛЬШЕ НЕ УГЛУБЛЯЙСЯ" in prompt


def test_format_for_prompt_when_complete_asks_for_closing_message():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")
    for topic in TOPIC_ORDER:
        service.mark_answered(
            tenant_id="t1", user_id="u1", topic=topic, summary="ok"
        )

    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    prompt = service.format_for_prompt(state)

    assert "Все темы пройдены" in prompt
    assert "onboarding_complete" in prompt


# ---------------------------------------------------------------------------
# Service: persistence round trip
# ---------------------------------------------------------------------------


def test_state_survives_service_recreation():
    """Two services on the same session see the same persisted state."""
    session = _fresh_session()
    svc1 = HousewifeOnboardingService(session)
    svc1.start(tenant_id="t1", user_id="u1")
    svc1.mark_answered(
        tenant_id="t1", user_id="u1",
        topic=TOPIC_ADDRESSING, summary="Борис",
    )

    svc2 = HousewifeOnboardingService(session)
    state = svc2.get_raw_state(tenant_id="t1", user_id="u1")
    assert state["status"] == STATUS_IN_PROGRESS
    assert state["topics"][TOPIC_ADDRESSING]["state"] == STATE_ANSWERED


def test_initialize_does_not_clobber_unrelated_skill_params():
    """If someone else wrote other keys into skill_params, initialize
    must preserve them — it only owns the ``onboarding`` subtree."""
    from sreda.db.repositories.user_profile import UserProfileRepository

    session = _fresh_session()
    UserProfileRepository(session).upsert_skill_config(
        "t1", "u1", HOUSEWIFE_FEATURE_KEY,
        source="user_command",
        skill_params={"some_unrelated_flag": True, "other": "value"},
    )
    session.commit()

    service = HousewifeOnboardingService(session)
    service.initialize(tenant_id="t1", user_id="u1")

    # Read back the full params.
    config = UserProfileRepository(session).get_skill_config(
        "t1", "u1", HOUSEWIFE_FEATURE_KEY
    )
    params = UserProfileRepository.decode_skill_params(config)
    assert params.get("some_unrelated_flag") is True
    assert params.get("other") == "value"
    assert "onboarding" in params
