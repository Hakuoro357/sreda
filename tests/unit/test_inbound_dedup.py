"""Dedup of Telegram updates by ``update_id`` — regression guard for
the 2026-04-22 prod double-reply incident. Long-poll / webhook retry
can re-deliver the same update; the second delivery must be detected
and short-circuited instead of firing a second chat turn.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User, Workspace
from sreda.services.inbound_messages import persist_telegram_inbound_event


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="tenant_1", name="Test"))
    sess.add(Workspace(id="ws_1", tenant_id="tenant_1", name="Default"))
    sess.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100"))
    sess.commit()
    yield sess
    sess.close()


def _payload(update_id: int, text: str = "привет") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": 100, "type": "private"},
            "text": text,
        },
    }


def test_first_delivery_not_flagged_duplicate(session):
    result = persist_telegram_inbound_event(
        session, bot_key="sreda", payload=_payload(42, "hi"),
    )
    assert result.is_duplicate is False
    assert result.inbound_message_id.startswith("in_")


def test_second_delivery_with_same_update_id_is_flagged_duplicate(session):
    """Telegram retry must land in the ``is_duplicate=True`` branch
    so the webhook handler can short-circuit before firing a second
    chat turn. The inbound_message_id stays the SAME — it points to
    the original record."""
    first = persist_telegram_inbound_event(
        session, bot_key="sreda", payload=_payload(42, "hi"),
    )
    assert first.is_duplicate is False

    # Same update_id arrives a second time. Maybe Telegram resent it
    # on network hiccup; maybe long-poll re-fetched it because the
    # offset commit lagged. Either way — must not look new.
    second = persist_telegram_inbound_event(
        session, bot_key="sreda", payload=_payload(42, "hi"),
    )
    assert second.is_duplicate is True
    assert second.inbound_message_id == first.inbound_message_id


def test_different_update_ids_both_fresh(session):
    """Two genuinely different updates in a row must both be treated
    as fresh — the dedup condition is an EXACT update_id match."""
    a = persist_telegram_inbound_event(
        session, bot_key="sreda", payload=_payload(100, "первое"),
    )
    b = persist_telegram_inbound_event(
        session, bot_key="sreda", payload=_payload(101, "второе"),
    )
    assert a.is_duplicate is False
    assert b.is_duplicate is False
    assert a.inbound_message_id != b.inbound_message_id


def test_payload_without_update_id_is_not_flagged_duplicate(session):
    """Some synthetic test payloads omit update_id entirely. Those
    must insert a fresh record each time (no dedup key to match on),
    but MUST NOT crash the pipeline."""
    bare = {
        "message": {
            "message_id": 1,
            "chat": {"id": 100, "type": "private"},
            "text": "no update_id here",
        },
    }
    a = persist_telegram_inbound_event(session, bot_key="sreda", payload=bare)
    b = persist_telegram_inbound_event(session, bot_key="sreda", payload=bare)
    assert a.is_duplicate is False
    assert b.is_duplicate is False
    # Distinct rows because we can't dedup without a key
    assert a.inbound_message_id != b.inbound_message_id
