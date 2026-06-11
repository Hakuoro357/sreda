"""#130 — смена стиля в СЕРЕДИНЕ жизни ≠ онбординг.

Прецедент Бориса 2026-06-11: «поменяй стиль общения» у давно живущего
пользователя отвечала онбординг-хвостом («Дальше можно сразу дать мне
задачу… посмотреть пару примеров» + кнопки) и оставляла висеть
«⚙️ Обрабатываю…». Чек-лист: (1) онбординг complete → короткое
подтверждение БЕЗ хвоста и БЕЗ кнопок; (2) онбординг не завершён →
прежнее поведение байт-в-байт; (3) перехват команды финализирует ack.
"""
from __future__ import annotations

import pytest

from sreda.services.housewife_persona import (
    PERSONA_SETTINGS_CALLBACK_PREFIX,
    PERSONA_TENDER_CARE,
    PERSONA_WARM_PRACTICAL,
    build_persona_choice_keyboard_max,
    build_persona_choice_keyboard_tg,
    build_persona_selected_message,
)


def test_midlife_messages_are_short_and_tailless() -> None:
    for preset in (PERSONA_TENDER_CARE, PERSONA_WARM_PRACTICAL):
        out = build_persona_selected_message(preset, in_onboarding=False)
        assert "Готово" in out
        assert "примеров" not in out
        assert "познаком" not in out
        assert "задачу" not in out
        assert len(out) < 60


def test_onboarding_messages_unchanged() -> None:
    out = build_persona_selected_message(PERSONA_TENDER_CARE)  # default=True
    assert "пару примеров" in out and "ласковый" in out


def test_settings_keyboard_uses_dedicated_prefix() -> None:
    """Codex R2 medium: источник различается ПРЕФИКСОМ колбэка, не
    статусом онбординга (post-approve welcome шлёт persona: при complete)."""
    kb = build_persona_choice_keyboard_tg(settings=True)
    for btn in kb["inline_keyboard"][0]:
        assert btn["callback_data"].startswith(PERSONA_SETTINGS_CALLBACK_PREFIX)
    kb_onb = build_persona_choice_keyboard_tg()
    for btn in kb_onb["inline_keyboard"][0]:
        assert btn["callback_data"].startswith("persona:")
        assert not btn["callback_data"].startswith("personaset:")
    kb_max = build_persona_choice_keyboard_max(settings=True)
    for btn in kb_max[0]["payload"]["buttons"][0]:
        assert btn["payload"].startswith(PERSONA_SETTINGS_CALLBACK_PREFIX)


@pytest.mark.asyncio
async def test_tg_callback_midlife_short_confirmation(monkeypatch) -> None:
    """Онбординг complete → правка сообщения коротким текстом без кнопок."""
    from test_housewife_persona_callbacks import FakeTelegramClient, session  # noqa: F401
    # пере-используем фикстуру руками (pytest импорт фикстур из модулей
    # соседей не подхватывает) — создаём окружение напрямую
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sreda.db.base import Base
    from sreda.db.models.core import Tenant, User, Workspace
    from sreda.services.onboarding import TelegramOnboardingResult
    from sreda.services.telegram_bot import _handle_callback

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(Workspace(id="w1", tenant_id="t1", name="W"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()

    client = FakeTelegramClient()
    onboarding = TelegramOnboardingResult(
        is_new_user=False, chat_id="42", tenant_id="t1",
        workspace_id="w1", user_id="u1", assistant_id="a1",
    )
    try:
        await _handle_callback(
            sess,
            telegram_client=client,
            callback_query={
                "id": "cb1",
                "data": f"personaset:{PERSONA_TENDER_CARE}",
                "message": {"message_id": 100},
            },
            onboarding=onboarding,
            bot_key="telegram_default",
            payload={},
            inbound_message_id=None,
        )
        assert client.edits, "ответ обязан быть правкой сообщения выбора"
        edit = client.edits[0]
        assert "Готово" in edit["text"]
        assert "примеров" not in edit["text"]
        assert edit.get("reply_markup") == {"inline_keyboard": []}, (
            "кнопки должны быть СНЯТЫ пустой клавиатурой (None их оставляет)"
        )
    finally:
        sess.close()
