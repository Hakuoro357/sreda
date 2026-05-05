"""Phase 3+10 — MAX inbound extractors + persist tests.

Real probe payloads (Phase 0 captured 2026-05-04):
- bot_started: chat_id top-level, user.user_id, user_id duplicated
- message_created: message.recipient.chat_id, message.body.{mid,text},
  message.sender.user_id

Lock-in: future probe-revisions of MAX API не должны silently сломать
наши extractors. Если структура поменяется — эти тесты упадут красным.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant
from sreda.services.inbound_messages import (
    _extract_max_chat_id,
    _extract_max_external_update_id,
    _extract_max_message_text,
    _extract_max_sender_user_id,
    persist_max_inbound_event,
)


# Real probe payload (Phase 0 — 2026-05-04, Boris wrote "Привет")
SAMPLE_MESSAGE_CREATED = {
    "message": {
        "recipient": {"chat_id": 320955459, "chat_type": "dialog", "user_id": 290524257},
        "timestamp": 1777907183208,
        "body": {
            "mid": "mid.0000000013216443019df386ae68000f",
            "seq": 116516925158719503,
            "text": "Привет",
        },
        "sender": {
            "user_id": 40921122, "first_name": "Борис", "last_name": "Печорин",
            "is_bot": False, "name": "Борис Печорин",
        },
    },
    "timestamp": 1777907183208,
    "user_locale": "ru",
    "update_type": "message_created",
}

SAMPLE_BOT_STARTED = {
    "timestamp": 1777907178400,
    "chat_id": 320955459,
    "user": {
        "user_id": 40921122, "first_name": "Борис", "last_name": "Печорин",
        "is_bot": False, "name": "Борис Печорин",
    },
    "user_locale": "ru",
    "user_id": 40921122,
    "update_type": "bot_started",
}

# Real probe payload (Phase 7 — 2026-05-05, Boris tapped "Готово ✅")
# CRITICAL: ``message.sender.user_id == 290524257`` is the **bot**, не Boris.
# ``callback.user.user_id == 40921122`` is the **юзер**. До 2026-05-05 PM
# extractor ошибочно возвращал bot_id, создавая orphan tenant_max_290524257.
SAMPLE_MESSAGE_CALLBACK = {
    "update_type": "message_callback",
    "timestamp": 1777994922729,
    "callback": {
        "timestamp": 1777994922729,
        "callback_id": "f9LHodD0cOLxkrlUxpeQAq",
        "user": {
            "user_id": 40921122, "first_name": "Борис", "last_name": "Печорин",
            "is_bot": False, "name": "Борис Печорин",
        },
        "payload": "test_done",
    },
    "message": {
        "recipient": {"chat_id": 320955459, "chat_type": "dialog", "user_id": 40921122},
        "timestamp": 1777994900000,
        "body": {
            "mid": "mid.000000001234abcd",
            "seq": 1,
            "text": "Тест inline кнопок",
            "attachments": [
                {"type": "inline_keyboard", "payload": {"buttons": [[
                    {"type": "callback", "text": "Готово ✅", "payload": "test_done"},
                ]]}},
            ],
        },
        "sender": {
            # КРИТИЧНО: sender для callback events — БОТ, не юзер
            "user_id": 290524257, "name": "Среда", "is_bot": True,
        },
    },
}


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def test_extract_chat_id_message_created():
    assert _extract_max_chat_id(SAMPLE_MESSAGE_CREATED) == 320955459


def test_extract_chat_id_bot_started():
    assert _extract_max_chat_id(SAMPLE_BOT_STARTED) == 320955459


def test_extract_chat_id_missing_returns_none():
    assert _extract_max_chat_id({"update_type": "noise"}) is None


def test_extract_sender_message_created():
    assert _extract_max_sender_user_id(SAMPLE_MESSAGE_CREATED) == 40921122


def test_extract_sender_bot_started():
    assert _extract_max_sender_user_id(SAMPLE_BOT_STARTED) == 40921122


def test_extract_sender_message_callback_returns_user_not_bot():
    """REGRESSION: до 2026-05-05 PM extractor ошибочно возвращал
    ``message.sender.user_id`` (= bot id 290524257) вместо
    ``callback.user.user_id`` (= human Boris id 40921122). Это создавало
    orphan tenant_max_290524257 при первом callback'е от любого юзера."""
    assert _extract_max_sender_user_id(SAMPLE_MESSAGE_CALLBACK) == 40921122
    # Защита от подмены поля sender в будущем — проверяем что bot_id
    # НЕ возвращается даже если очень захочется.
    assert _extract_max_sender_user_id(SAMPLE_MESSAGE_CALLBACK) != 290524257


def test_extract_chat_id_message_callback():
    """Callback events carry recipient.chat_id для context — extractor
    должен достать его, не возвращать None."""
    assert _extract_max_chat_id(SAMPLE_MESSAGE_CALLBACK) == 320955459


def test_extract_message_text_present():
    assert _extract_max_message_text(SAMPLE_MESSAGE_CREATED) == "Привет"


def test_extract_message_text_no_message_field():
    assert _extract_max_message_text(SAMPLE_BOT_STARTED) is None


def test_extract_message_text_empty_body_returns_none():
    payload = {"message": {"body": {"text": "   "}}}
    assert _extract_max_message_text(payload) is None


# ---------------------------------------------------------------------------
# external_update_id (dedup key)
# ---------------------------------------------------------------------------


def test_external_update_id_uses_mid_for_messages():
    assert _extract_max_external_update_id(SAMPLE_MESSAGE_CREATED) == \
        "mid.0000000013216443019df386ae68000f"


def test_external_update_id_synthetic_for_bot_started():
    eid = _extract_max_external_update_id(SAMPLE_BOT_STARTED)
    assert eid == "max:bot_started:320955459:1777907178400"


def test_external_update_id_returns_none_for_garbage():
    assert _extract_max_external_update_id({"random": "stuff"}) is None


def test_external_update_id_uses_callback_id_for_callback_events():
    """Callback events have unique callback_id per tap — dedup ключ
    должен использовать его, чтобы повторный tap (новый callback_id) не
    schлоп'нулся под одним body.mid с предыдущим tap'ом."""
    eid = _extract_max_external_update_id(SAMPLE_MESSAGE_CALLBACK)
    assert eid == "max:cb:f9LHodD0cOLxkrlUxpeQAq"
    # Проверяем что mid НЕ используется для callback events — иначе два
    # разных tap'а на одной кнопке (разные callback_id, тот же mid) дали
    # бы duplicate.
    assert "mid" not in eid


# ---------------------------------------------------------------------------
# persist_max_inbound_event idempotency
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T1"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def test_persist_max_inbound_creates_row(session):
    result = persist_max_inbound_event(
        session, bot_key="sreda_max", payload=SAMPLE_MESSAGE_CREATED,
    )
    assert result.is_duplicate is False
    assert result.update_type == "message_created"
    assert result.inbound_message_id.startswith("in_")


def test_persist_max_inbound_dedups_duplicate(session):
    """Same body.mid → second persist returns existing row."""
    first = persist_max_inbound_event(
        session, bot_key="sreda_max", payload=SAMPLE_MESSAGE_CREATED,
    )
    second = persist_max_inbound_event(
        session, bot_key="sreda_max", payload=SAMPLE_MESSAGE_CREATED,
    )
    assert second.is_duplicate is True
    assert second.inbound_message_id == first.inbound_message_id


def test_persist_max_inbound_bot_started_synthetic_dedup(session):
    """Same bot_started (chat_id + ts) → second persist is duplicate."""
    first = persist_max_inbound_event(
        session, bot_key="sreda_max", payload=SAMPLE_BOT_STARTED,
    )
    second = persist_max_inbound_event(
        session, bot_key="sreda_max", payload=SAMPLE_BOT_STARTED,
    )
    assert second.is_duplicate is True
    assert second.inbound_message_id == first.inbound_message_id
