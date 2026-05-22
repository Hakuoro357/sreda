from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.user_profile import TenantUserProfile
from sreda.db.models.core import Tenant, User
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.services.housewife_persona import (
    PERSONA_TENDER_CARE,
    PERSONA_WARM_PRACTICAL,
)
from sreda.services.max_inbound import (
    _handle_max_callback,
    _handle_max_pending_tenant,
    _handle_max_persona_settings_request,
)
from sreda.services.onboarding import MaxOnboardingResult, TelegramOnboardingResult
from sreda.services.telegram_bot import _handle_callback, handle_telegram_interaction


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


class FakeTelegramClient:
    def __init__(self) -> None:
        self.answered: list[tuple[str, str | None]] = []
        self.edits: list[dict] = []
        self.sends: list[dict] = []

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None,
    ) -> dict:
        self.answered.append((callback_query_id, text))
        return {"ok": True}

    async def edit_message_text(self, **kwargs) -> dict:
        self.edits.append(kwargs)
        return {"ok": True}

    async def send_message(self, **kwargs) -> dict:
        self.sends.append(kwargs)
        return {"ok": True}


class FakeMaxClient:
    def __init__(self) -> None:
        self.answered: list[tuple[str, str | None]] = []
        self.edits: list[dict] = []
        self.sends: list[dict] = []

    async def answer_callback(
        self, callback_id: str, notification: str | None = None,
    ) -> dict:
        self.answered.append((callback_id, notification))
        return {"success": True}

    async def edit_message(self, message_id: str, **kwargs) -> dict:
        self.edits.append({"message_id": message_id, **kwargs})
        return {"success": True}

    async def send_message(self, **kwargs) -> dict:
        self.sends.append(kwargs)
        return {"success": True}


@pytest.mark.asyncio
async def test_telegram_persona_callback_stores_preset_and_edits_message(
    session,
) -> None:
    client = FakeTelegramClient()
    onboarding = TelegramOnboardingResult(
        is_new_user=False,
        chat_id="42",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        assistant_id="a1",
    )

    await _handle_callback(
        session,
        telegram_client=client,
        callback_query={
            "id": "cb1",
            "data": f"persona:{PERSONA_TENDER_CARE}",
            "message": {"message_id": 100},
        },
        onboarding=onboarding,
        bot_key="telegram_default",
        payload={},
        inbound_message_id=None,
    )

    repo = UserProfileRepository(session)
    cfg = repo.get_skill_config("t1", "u1", "housewife_assistant")
    assert cfg is not None
    params = UserProfileRepository.decode_skill_params(cfg)
    assert params["persona_preset"] == PERSONA_TENDER_CARE
    assert client.answered == [("cb1", "")]
    assert client.sends == []
    assert client.edits[0]["message_id"] == 100
    assert "ласковый" in client.edits[0]["text"]
    assert "пару примеров" in client.edits[0]["text"]
    buttons = client.edits[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["text"] == "Покажи примеры"
    assert buttons[0]["callback_data"] == "pb:voice"
    assert buttons[1]["callback_data"] == "persona_ready"


@pytest.mark.asyncio
async def test_max_persona_callback_stores_preset_and_edits_message(
    session,
) -> None:
    client = FakeMaxClient()
    onboarding = MaxOnboardingResult(
        is_new_user=False,
        max_account_id="max1",
        max_chat_id="chat1",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        assistant_id="a1",
    )

    handled = await _handle_max_callback(
        session=session,
        max_client=client,
        payload={
            "callback": {
                "callback_id": "cb1",
                "payload": f"persona:{PERSONA_WARM_PRACTICAL}",
            },
            "message": {"body": {"mid": "mid1"}},
        },
        onboarding=onboarding,
    )

    repo = UserProfileRepository(session)
    cfg = repo.get_skill_config("t1", "u1", "housewife_assistant")
    assert handled is True
    assert cfg is not None
    params = UserProfileRepository.decode_skill_params(cfg)
    assert params["persona_preset"] == PERSONA_WARM_PRACTICAL
    assert client.sends == []
    assert client.edits[0]["message_id"] == "mid1"
    assert "спокойный" in client.edits[0]["text"]
    assert "пару примеров" in client.edits[0]["text"]
    buttons = client.edits[0]["attachments"][0]["payload"]["buttons"][0]
    assert buttons[0]["text"] == "Покажи примеры"
    assert buttons[0]["payload"] == "pb:voice"
    assert buttons[1]["payload"] == "persona_ready"
    assert client.answered == [("cb1", "")]


@pytest.mark.asyncio
async def test_telegram_persona_ready_callback_edits_message_and_removes_keyboard(
    session,
) -> None:
    client = FakeTelegramClient()
    onboarding = TelegramOnboardingResult(
        is_new_user=False,
        chat_id="42",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        assistant_id="a1",
    )

    await _handle_callback(
        session,
        telegram_client=client,
        callback_query={
            "id": "cb_ready",
            "data": "persona_ready",
            "message": {"message_id": 101},
        },
        onboarding=onboarding,
        bot_key="telegram_default",
        payload={},
        inbound_message_id=None,
    )

    assert client.answered == [("cb_ready", "")]
    assert client.sends == []
    assert client.edits[0]["message_id"] == 101
    assert "пиши или говори голосом" in client.edits[0]["text"].lower()
    assert client.edits[0]["reply_markup"] == {"inline_keyboard": []}


@pytest.mark.asyncio
async def test_max_persona_ready_callback_edits_message_and_removes_keyboard(
    session,
) -> None:
    client = FakeMaxClient()
    onboarding = MaxOnboardingResult(
        is_new_user=False,
        max_account_id="max1",
        max_chat_id="chat1",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        assistant_id="a1",
    )

    handled = await _handle_max_callback(
        session=session,
        max_client=client,
        payload={
            "callback": {
                "callback_id": "cb_ready",
                "payload": "persona_ready",
            },
            "message": {"body": {"mid": "mid_ready"}},
        },
        onboarding=onboarding,
    )

    assert handled is True
    assert client.answered == [("cb_ready", "")]
    assert client.sends == []
    assert client.edits[0]["message_id"] == "mid_ready"
    assert "пиши или говори голосом" in client.edits[0]["text"].lower()
    assert client.edits[0]["attachments"] == []


@pytest.mark.asyncio
async def test_telegram_persona_settings_request_sends_choice_keyboard(
    session,
) -> None:
    client = FakeTelegramClient()
    onboarding = TelegramOnboardingResult(
        is_new_user=False,
        chat_id="42",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        assistant_id="a1",
    )

    await handle_telegram_interaction(
        session,
        bot_key="telegram_default",
        payload={
            "message": {
                "chat": {"id": 42},
                "text": "поменяй стиль общения",
            }
        },
        telegram_client=client,
        onboarding=onboarding,
        inbound_message_id="in1",
    )

    assert client.edits == []
    assert len(client.sends) == 1
    assert "Выбери, как мне с тобой общаться" in client.sends[0]["text"]
    assert "веду списки твоих дел" in client.sends[0]["text"]
    assert "ищу в интернете нужную тебе информацию" in client.sends[0]["text"]
    buttons = client.sends[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"persona:{PERSONA_WARM_PRACTICAL}"
    assert buttons[1]["callback_data"] == f"persona:{PERSONA_TENDER_CARE}"


@pytest.mark.asyncio
async def test_max_persona_settings_request_sends_choice_keyboard() -> None:
    client = FakeMaxClient()
    onboarding = MaxOnboardingResult(
        is_new_user=False,
        max_account_id="max1",
        max_chat_id="chat1",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        assistant_id="a1",
    )

    await _handle_max_persona_settings_request(
        max_client=client,
        onboarding=onboarding,
    )

    assert client.edits == []
    assert len(client.sends) == 1
    assert client.sends[0]["recipient"] == {"chat_id": "chat1"}
    assert "Выбери, как мне с тобой общаться" in client.sends[0]["text"]
    assert "веду списки твоих дел" in client.sends[0]["text"]
    buttons = client.sends[0]["attachments"][0]["payload"]["buttons"][0]
    assert buttons[0]["payload"] == f"persona:{PERSONA_WARM_PRACTICAL}"
    assert buttons[1]["payload"] == f"persona:{PERSONA_TENDER_CARE}"


@pytest.mark.asyncio
async def test_telegram_post_tour_name_reply_is_saved_without_llm(
    session, monkeypatch,
) -> None:
    client = FakeTelegramClient()
    onboarding = TelegramOnboardingResult(
        is_new_user=False,
        chat_id="42",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        assistant_id="a1",
    )

    from sreda.services.housewife_onboarding import record_pb_tour_progress

    record_pb_tour_progress(
        session,
        tenant_id="t1",
        user_id="u1",
        branch="done",
    )
    session.commit()

    async def fail_if_llm_called(*args, **kwargs):
        raise AssertionError("name reply must not be routed to LLM")

    monkeypatch.setattr(
        "sreda.services.telegram_bot._handle_command",
        fail_if_llm_called,
    )

    await handle_telegram_interaction(
        session,
        bot_key="telegram_default",
        payload={
            "message": {
                "chat": {"id": 42},
                "text": "Меня зовут Борис Аркадьевич",
            }
        },
        telegram_client=client,
        onboarding=onboarding,
        inbound_message_id="in_name",
    )

    profile = session.query(TenantUserProfile).filter_by(
        tenant_id="t1", user_id="u1",
    ).one()
    assert profile.display_name == "Борис Аркадьевич"
    assert client.edits == []
    assert len(client.sends) == 1
    assert "Запомнила" in client.sends[0]["text"]
    assert "Борис Аркадьевич" in client.sends[0]["text"]
    assert "что будем делать первым" in client.sends[0]["text"]
    assert "reply_markup" not in client.sends[0]


@pytest.mark.asyncio
async def test_max_pb_done_records_name_waiting_flag(
    session, monkeypatch,
) -> None:
    from sreda.services.housewife_onboarding import (
        is_pb_tour_waiting_for_name,
    )

    client = FakeMaxClient()
    onboarding = MaxOnboardingResult(
        is_new_user=False,
        max_account_id="max1",
        max_chat_id="chat1",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        assistant_id="a1",
    )

    class _Settings:
        max_bot_token = "max-token"

    monkeypatch.setattr(
        "sreda.integrations.max.client.MaxClient",
        lambda token: client,
    )

    await _handle_max_pending_tenant(
        session=session,
        payload={
            "update_type": "message_callback",
            "callback": {"callback_id": "cb_done", "payload": "pb:done"},
            "message": {"body": {"mid": "mid_done"}},
        },
        update_type="message_callback",
        onboarding=onboarding,
        settings=_Settings(),
        is_post_approve_tour=True,
    )

    assert client.answered == [("cb_done", None)]
    assert client.edits[0]["message_id"] == "mid_done"
    assert "Как мне к тебе обращаться" in client.edits[0]["text"]
    assert is_pb_tour_waiting_for_name(
        session,
        tenant_id="t1",
        user_id="u1",
    )
