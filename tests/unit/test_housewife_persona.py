from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.services.channel_linking import consume_link, start_link
from sreda.services.housewife_persona import (
    DEFAULT_PERSONA_PRESET,
    PERSONA_TENDER_CARE,
    PERSONA_WARM_PRACTICAL,
    build_persona_choice_keyboard_max,
    build_persona_choice_keyboard_tg,
    build_persona_choice_message,
    build_persona_overlay,
    get_persona_preset,
    is_persona_settings_request,
    normalize_persona_preset,
    set_persona_preset,
)
from sreda.services.onboarding import (
    ensure_max_user_bundle,
    ensure_telegram_user_bundle,
    find_user_by_chat_id,
    find_user_by_max_account_id,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.checklists  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401

    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Tenant 1"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture(autouse=True)
def _identity_phase_uses_session(session, monkeypatch):
    """#138 Ф5-5c: резолв/провижн/tenant-чтения открывают свои privileged/
    tenant сессии; в юните стабаем обе на sqlite-сессию теста + сеем sreda_free."""
    from contextlib import contextmanager

    import sreda.db.session as dbs
    from sreda.db.models.billing import SubscriptionPlan

    if session.query(SubscriptionPlan).filter_by(plan_key="sreda_free").first() is None:
        session.add(SubscriptionPlan(
            id="plan_free", plan_key="sreda_free",
            feature_key="housewife_assistant", title="Free", description="", price_rub=0,
        ))
        session.commit()

    @contextmanager
    def _stub(arg):
        yield session

    monkeypatch.setattr(dbs, "privileged_session", _stub)
    monkeypatch.setattr(dbs, "tenant_session", _stub)


def test_normalize_persona_defaults_unknown_values() -> None:
    assert normalize_persona_preset(None) == DEFAULT_PERSONA_PRESET
    assert normalize_persona_preset("") == DEFAULT_PERSONA_PRESET
    assert normalize_persona_preset("unknown") == DEFAULT_PERSONA_PRESET
    assert normalize_persona_preset(PERSONA_TENDER_CARE) == PERSONA_TENDER_CARE


def test_get_persona_preset_defaults_when_skill_config_missing(session) -> None:
    assert get_persona_preset(session, tenant_id="t1", user_id="u1") == (
        PERSONA_WARM_PRACTICAL
    )


def test_set_persona_preset_preserves_existing_skill_params(session) -> None:
    repo = UserProfileRepository(session)
    repo.upsert_skill_config(
        "t1",
        "u1",
        "housewife_assistant",
        skill_params={
            "onboarding": {"status": "in_progress"},
            "welcome_sent": True,
        },
    )
    session.commit()

    set_persona_preset(
        session,
        tenant_id="t1",
        user_id="u1",
        preset=PERSONA_TENDER_CARE,
        source="user_command",
    )
    session.commit()

    cfg = repo.get_skill_config("t1", "u1", "housewife_assistant")
    assert cfg is not None
    params = UserProfileRepository.decode_skill_params(cfg)
    assert params["persona_preset"] == PERSONA_TENDER_CARE
    assert params["onboarding"] == {"status": "in_progress"}
    assert params["welcome_sent"] is True
    assert get_persona_preset(session, tenant_id="t1", user_id="u1") == (
        PERSONA_TENDER_CARE
    )


def test_set_persona_preset_rejects_unknown_preset(session) -> None:
    with pytest.raises(ValueError):
        set_persona_preset(
            session,
            tenant_id="t1",
            user_id="u1",
            preset="too_much_sugar",
        )


def test_linked_tg_and_max_channels_share_single_persona(session) -> None:
    user = session.get(User, "u1")
    user.max_account_id = "max42"
    user.max_chat_id = "chat42"
    session.commit()

    tg_user = find_user_by_chat_id(session, "42")
    max_user = find_user_by_max_account_id(session, "max42")
    assert tg_user is not None
    assert max_user is not None
    assert tg_user.id == max_user.id == "u1"
    assert tg_user.tenant_id == max_user.tenant_id == "t1"

    tg_onboarding = ensure_telegram_user_bundle(
        session,
        {"message": {"chat": {"id": 42}, "from": {"first_name": "Boris"}}},
    )
    max_onboarding = ensure_max_user_bundle(
        session,
        max_account_id="max42",
        max_chat_id="chat42",
        display_name="Boris",
    )
    assert tg_onboarding.user_id == max_onboarding.user_id == "u1"
    assert tg_onboarding.tenant_id == max_onboarding.tenant_id == "t1"

    set_persona_preset(
        session,
        tenant_id=max_onboarding.tenant_id,
        user_id=max_onboarding.user_id,
        preset=PERSONA_TENDER_CARE,
        source="user_command",
    )
    session.commit()
    assert get_persona_preset(
        session,
        tenant_id=tg_onboarding.tenant_id,
        user_id=tg_onboarding.user_id,
    ) == PERSONA_TENDER_CARE

    set_persona_preset(
        session,
        tenant_id=tg_onboarding.tenant_id,
        user_id=tg_onboarding.user_id,
        preset=PERSONA_WARM_PRACTICAL,
        source="user_command",
    )
    session.commit()
    assert get_persona_preset(
        session,
        tenant_id=max_onboarding.tenant_id,
        user_id=max_onboarding.user_id,
    ) == PERSONA_WARM_PRACTICAL


def test_channel_linking_keeps_existing_persona_single_scoped(session) -> None:
    set_persona_preset(
        session,
        tenant_id="t1",
        user_id="u1",
        preset=PERSONA_TENDER_CARE,
        source="user_command",
    )
    session.commit()

    link = start_link(
        session,
        tenant_id="t1",
        source_channel="telegram",
        source_user_id="u1",
    )
    outcome = consume_link(
        session,
        raw_token=link.raw_token,
        target_channel="max",
        target_account_id="max42",
        target_chat_id="chat42",
    )
    assert outcome.success is True
    assert outcome.tenant_id == "t1"

    max_onboarding = ensure_max_user_bundle(
        session,
        max_account_id="max42",
        max_chat_id="chat42",
        display_name="Boris",
    )
    assert max_onboarding.tenant_id == "t1"
    assert max_onboarding.user_id == "u1"
    assert get_persona_preset(
        session,
        tenant_id=max_onboarding.tenant_id,
        user_id=max_onboarding.user_id,
    ) == PERSONA_TENDER_CARE


def test_max_to_tg_channel_linking_keeps_existing_persona_single_scoped(
    session,
) -> None:
    user = session.get(User, "u1")
    user.telegram_account_id = None
    user.tg_account_hash = None
    user.max_account_id = "max42"
    user.max_chat_id = "chat42"
    session.commit()

    set_persona_preset(
        session,
        tenant_id="t1",
        user_id="u1",
        preset=PERSONA_TENDER_CARE,
        source="user_command",
    )
    session.commit()

    link = start_link(
        session,
        tenant_id="t1",
        source_channel="max",
        source_user_id="u1",
        tg_bot_username="sreda_test_bot",
        tg_miniapp_shortname="app",
    )
    outcome = consume_link(
        session,
        raw_token=link.raw_token,
        target_channel="telegram",
        target_account_id="42",
    )
    assert outcome.success is True
    assert outcome.tenant_id == "t1"

    tg_onboarding = ensure_telegram_user_bundle(
        session,
        {"message": {"chat": {"id": 42}, "from": {"first_name": "Boris"}}},
    )
    assert tg_onboarding.tenant_id == "t1"
    assert tg_onboarding.user_id == "u1"
    assert get_persona_preset(
        session,
        tenant_id=tg_onboarding.tenant_id,
        user_id=tg_onboarding.user_id,
    ) == PERSONA_TENDER_CARE


def test_persona_overlays_are_distinct() -> None:
    warm = build_persona_overlay(PERSONA_WARM_PRACTICAL)
    tender = build_persona_overlay(PERSONA_TENDER_CARE)

    assert "warm_practical" in warm
    assert "tender_care" in tender
    assert "солнышко" not in warm
    assert "солнышко" in tender
    assert "базовому характеру" in warm
    assert "базовому характеру" in tender


def test_persona_choice_message_and_keyboards() -> None:
    message = build_persona_choice_message()
    assert "Я Среда" in message
    assert "Помогаю с бытовой рутиной" in message
    assert "• 🎙 понимаю голос — можешь говорить или писать сообщения" in message
    assert "• ⏰ ставлю напоминания" in message
    assert "• 🛒 веду список покупок" in message
    assert "• 📋 веду списки твоих дел" in message
    assert "• 🍽 составляю меню на неделю" in message
    assert "• 📖 сохраняю и нахожу рецепты" in message
    assert "• 👨‍👩‍👧 запоминаю важное про семью" in message
    assert "• 🌤 могу подсказать погоду" in message
    assert "• 🔍 ищу в интернете нужную тебе информацию" in message
    assert "помню факты" not in message

    tg = build_persona_choice_keyboard_tg()
    tg_buttons = tg["inline_keyboard"][0]
    assert tg_buttons[0]["callback_data"] == f"persona:{PERSONA_WARM_PRACTICAL}"
    assert tg_buttons[1]["callback_data"] == f"persona:{PERSONA_TENDER_CARE}"

    max_keyboard = build_persona_choice_keyboard_max()
    max_buttons = max_keyboard[0]["payload"]["buttons"][0]
    assert max_buttons[0]["payload"] == f"persona:{PERSONA_WARM_PRACTICAL}"
    assert max_buttons[1]["payload"] == f"persona:{PERSONA_TENDER_CARE}"


def test_persona_settings_request_detection() -> None:
    assert is_persona_settings_request("поменяй стиль общения")
    assert is_persona_settings_request("Настрой стиль общения")
    assert is_persona_settings_request("хочу выбрать личность")
    assert is_persona_settings_request("сменить личность помощницы")
    assert not is_persona_settings_request("поставь напоминание на завтра")
