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
    _extract_short_name,
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


def test_next_topic_addressing_pending_returns_addressing():
    """2026-04-27: TOPIC_ORDER = (TOPIC_ADDRESSING,). Если addressing
    pending — он же current."""
    state = {
        "topics": {
            TOPIC_ADDRESSING: {"state": STATE_PENDING},
        }
    }
    assert _next_topic(state["topics"]) == TOPIC_ADDRESSING


def test_next_topic_skipped_once_returns_for_second_pass():
    """skipped_once темы возвращаются на повторный показ. Для одной
    темы — это сама addressing после первого defer."""
    state = {
        "topics": {
            TOPIC_ADDRESSING: {"state": STATE_SKIPPED_ONCE},
        }
    }
    assert _next_topic(state["topics"]) == TOPIC_ADDRESSING


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


def test_mark_answered_saves_summary_and_completes():
    """2026-04-27: TOPIC_ORDER = (addressing,). После ответа на
    единственную тему — current_topic становится None (все темы
    пройдены)."""
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
    # Все темы пройдены, current_topic None.
    assert state["current_topic"] is None


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


def test_mark_deferred_once_moves_to_skipped_once_for_retry():
    """2026-04-27: TOPIC_ORDER = (addressing,). После defer'а единственной
    темы она в skipped_once и остаётся current — ждёт второго шанса."""
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    service.mark_deferred(
        tenant_id="t1", user_id="u1", topic=TOPIC_ADDRESSING
    )
    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    assert state["topics"][TOPIC_ADDRESSING]["state"] == STATE_SKIPPED_ONCE
    # Единственная тема, после первого defer — она же current
    # (для повторной попытки).
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


def test_format_for_prompt_shows_current_topic_at_start():
    """До ответа на addressing prompt показывает её как current и
    содержит tool-call hints для LLM."""
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    prompt = service.format_for_prompt(state)

    # Current topic — addressing.
    assert TOPIC_ADDRESSING in prompt
    # Pending marker (юзер ещё не ответил).
    assert "⏸" in prompt
    # Tool call hints для LLM.
    assert "onboarding_answered" in prompt
    assert "onboarding_deferred" in prompt


def test_format_for_prompt_shows_completion_after_addressing_answered():
    """После ответа на единственную тему — current_topic None и
    prompt сообщает что все темы пройдены."""
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")
    service.mark_answered(
        tenant_id="t1", user_id="u1",
        topic=TOPIC_ADDRESSING, summary="Борис",
    )

    state = service.get_raw_state(tenant_id="t1", user_id="u1")
    prompt = service.format_for_prompt(state)

    # Сохранённый ответ показан с маркером.
    assert "✅" in prompt
    assert "Борис" in prompt
    # Все темы пройдены — закрывающий blurb.
    assert "Все темы пройдены" in prompt


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
    """Two services on the same session see the same persisted state.
    2026-04-27: с TOPIC_ORDER=(addressing,) после ответа single topic
    статус становится `complete` (нет других тем для сбора)."""
    session = _fresh_session()
    svc1 = HousewifeOnboardingService(session)
    svc1.start(tenant_id="t1", user_id="u1")
    svc1.mark_answered(
        tenant_id="t1", user_id="u1",
        topic=TOPIC_ADDRESSING, summary="Борис",
    )

    svc2 = HousewifeOnboardingService(session)
    state = svc2.get_raw_state(tenant_id="t1", user_id="u1")
    # После единственной темы — flow закончен.
    assert state["status"] == STATUS_COMPLETE
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


# ---------------------------------------------------------------------------
# _extract_short_name — defense against LLM stuffing full sentences as names
# ---------------------------------------------------------------------------
#
# 2026-04-28 incident (см. docs/copy/welcome.md): LLM, передавая
# `summary` в `onboarding_answered(topic="addressing")`, иногда сохраняет
# целую фразу вместо имени. На проде нашли:
#   * "Пользователя зовут Борис."
#   * "Пользователя зовут Повелитель — просил называть его так."
#   * "Пользователь хочет, чтобы его называли «Шеф»."
# Эти строки попадали в `display_name`, и в LLM-prompt'е выглядело как
# `Имя: Пользователя зовут Борис.` Защита: helper, который чистит
# распространённые префиксы и обрезает результат до короткого имени.


def test_extract_short_name_returns_short_input_unchanged():
    assert _extract_short_name("Борис") == "Борис"
    assert _extract_short_name("Анна") == "Анна"
    assert _extract_short_name("Кэт") == "Кэт"


def test_extract_short_name_strips_polzovatel_zovut_prefix():
    assert _extract_short_name("Пользователя зовут Борис.") == "Борис"
    assert _extract_short_name("Пользователя зовут Анна") == "Анна"


def test_extract_short_name_strips_menya_zovut_prefix():
    assert _extract_short_name("Меня зовут Борис") == "Борис"
    assert _extract_short_name("меня зовут Анна Викторовна") == "Анна Викторовна"


def test_extract_short_name_strips_zovi_menya_prefix():
    assert _extract_short_name("Зови меня Кэт") == "Кэт"
    assert _extract_short_name("зови меня кошечкой") == "кошечкой"


def test_extract_short_name_handles_polzovatel_hochet_phrase():
    """«Пользователь хочет, чтобы его называли «Шеф».» → «Шеф»."""
    raw = "Пользователь хочет, чтобы его называли «Шеф»."
    assert _extract_short_name(raw) == "Шеф"


def test_extract_short_name_truncates_with_explanation_clause():
    """«Пользователя зовут Повелитель — просил называть его так.» → «Повелитель»."""
    raw = "Пользователя зовут Повелитель — просил называть его так."
    assert _extract_short_name(raw) == "Повелитель"


def test_extract_short_name_strips_quotes_and_terminal_punct():
    assert _extract_short_name("«Шеф»") == "Шеф"
    assert _extract_short_name("'Борис'") == "Борис"
    assert _extract_short_name("Анна.") == "Анна"
    assert _extract_short_name("Анна,") == "Анна"


def test_extract_short_name_empty_input_returns_empty():
    assert _extract_short_name("") == ""
    assert _extract_short_name("   ") == ""
    assert _extract_short_name(None) == ""  # type: ignore[arg-type]


def test_extract_short_name_caps_at_30_chars():
    """Длинные несжимаемые строки обрезаются до 30 — это всё ещё мусор,
    но не полная портянка, и validate_proposed_field в handlers.py
    отрежет такое в дальнейшем при ужесточении."""
    raw = "Очень длинное предложение которое в имя точно не лезет"
    assert len(_extract_short_name(raw)) <= 30


def test_mark_answered_addressing_sanitizes_display_name():
    """LLM передаёт full-sentence summary → mirror в display_name
    должен быть очищен через _extract_short_name. Это последняя линия
    обороны — даже если LLM не послушал docstring."""
    from sreda.db.repositories.user_profile import UserProfileRepository

    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    service.mark_answered(
        tenant_id="t1",
        user_id="u1",
        topic=TOPIC_ADDRESSING,
        summary="Пользователя зовут Борис.",
    )

    profile = UserProfileRepository(session).get_profile("t1", "u1")
    assert profile is not None
    assert profile.display_name == "Борис"


def test_mark_answered_addressing_sanitizes_polzovatel_hochet_phrase():
    from sreda.db.repositories.user_profile import UserProfileRepository

    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.start(tenant_id="t1", user_id="u1")

    service.mark_answered(
        tenant_id="t1",
        user_id="u1",
        topic=TOPIC_ADDRESSING,
        summary="Пользователь хочет, чтобы его называли «Шеф».",
    )

    profile = UserProfileRepository(session).get_profile("t1", "u1")
    assert profile is not None
    assert profile.display_name == "Шеф"
