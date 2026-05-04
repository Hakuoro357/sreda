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
