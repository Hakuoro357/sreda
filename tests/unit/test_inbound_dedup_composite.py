"""Phase 2 dedup tests: composite key (channel_type, bot_key, external_update_id).

Verifies that:
- Same update_id from two different Telegram bots → both accepted (no false-dedup).
- Same update_id across channels (telegram vs max) → both accepted (no collision).
- Repeat of the exact same (channel, bot_key, update_id) → deduped (is_duplicate=True).
- The SQLite schema built from Base.metadata carries the partial unique index
  so these invariants are enforced at the DB level too.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import sreda.db.models  # noqa: F401 — register core tables
import sreda.db.models.audit  # noqa: F401
import sreda.db.models.checklists  # noqa: F401
import sreda.db.models.free_tier  # noqa: F401
import sreda.db.models.reply_buttons  # noqa: F401
from sreda.db.base import Base
from sreda.db.models.core import Tenant, User, Workspace
from sreda.services.inbound_messages import (
    persist_max_inbound_event,
    persist_telegram_inbound_event,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    """In-memory SQLite engine with schema built from ORM metadata."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """Per-test session with rollback teardown."""
    conn = engine.connect()
    trans = conn.begin()
    sess = sessionmaker(bind=conn)()
    # Minimal seed: one tenant/workspace/user referenced by inbound helpers.
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(Workspace(id="ws1", tenant_id="t1", name="WS"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="999"))
    sess.commit()
    yield sess
    sess.close()
    trans.rollback()
    conn.close()


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _tg_payload(update_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": 999, "type": "private"},
            "text": "hello",
        },
    }


def _max_payload(mid: str) -> dict:
    return {
        "update_type": "message_created",
        "message": {
            "body": {"text": "hi", "mid": mid, "seq": 1},
            "sender": {"user_id": "mx_user_1"},
            "recipient": {"chat_id": "mx_chat_1"},
        },
    }


# ---------------------------------------------------------------------------
# Tests: cross-bot non-collision
# ---------------------------------------------------------------------------


def test_same_update_id_different_bots_both_accepted(session):
    """Bot-A and bot-B both deliver update_id=100.  Must produce two distinct
    inbound records — not a false duplicate."""
    r_a = persist_telegram_inbound_event(
        session, bot_key="sreda", payload=_tg_payload(100),
    )
    r_b = persist_telegram_inbound_event(
        session, bot_key="sreda_home", payload=_tg_payload(100),
    )
    assert r_a.is_duplicate is False
    assert r_b.is_duplicate is False
    assert r_a.inbound_message_id != r_b.inbound_message_id


# ---------------------------------------------------------------------------
# Tests: cross-channel non-collision
# ---------------------------------------------------------------------------


def test_same_update_id_different_channels_both_accepted(session):
    """Telegram update_id 200 and MAX mid '200' must not collide even if
    the string representations are identical."""
    r_tg = persist_telegram_inbound_event(
        session, bot_key="sreda", payload=_tg_payload(200),
    )
    r_max = persist_max_inbound_event(
        session, bot_key="sreda_max", payload=_max_payload("200"),
    )
    assert r_tg.is_duplicate is False
    assert r_max.is_duplicate is False
    assert r_tg.inbound_message_id != r_max.inbound_message_id


# ---------------------------------------------------------------------------
# Tests: within-bot dedup preserved
# ---------------------------------------------------------------------------


def test_same_bot_same_update_id_is_deduped(session):
    """Telegram retry for (sreda, update_id=300) must still be deduped — the
    existing same-bot behaviour must be unchanged."""
    first = persist_telegram_inbound_event(
        session, bot_key="sreda", payload=_tg_payload(300),
    )
    second = persist_telegram_inbound_event(
        session, bot_key="sreda", payload=_tg_payload(300),
    )
    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.inbound_message_id == first.inbound_message_id


def test_same_bot_home_same_update_id_is_deduped(session):
    """Telegram retry for (sreda_home, update_id=300) must also be deduped
    independently of the sreda-bot rows."""
    first = persist_telegram_inbound_event(
        session, bot_key="sreda_home", payload=_tg_payload(300),
    )
    second = persist_telegram_inbound_event(
        session, bot_key="sreda_home", payload=_tg_payload(300),
    )
    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.inbound_message_id == first.inbound_message_id


def test_max_same_mid_same_bot_is_deduped(session):
    """MAX retry for (sreda_max, mid='mid-42') must be deduped."""
    first = persist_max_inbound_event(
        session, bot_key="sreda_max", payload=_max_payload("mid-42"),
    )
    second = persist_max_inbound_event(
        session, bot_key="sreda_max", payload=_max_payload("mid-42"),
    )
    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.inbound_message_id == first.inbound_message_id


# ---------------------------------------------------------------------------
# Test: index exists on the SQLite schema (compile-level assertion)
# ---------------------------------------------------------------------------


def test_partial_unique_index_present_in_sqlite_schema(engine):
    """The ORM __table_args__ must create the composite partial unique index
    when Base.metadata.create_all is used (the code-path for unit tests).
    SQLAlchemy's Inspector exposes indexes on the table."""
    inspector = inspect(engine)
    indexes = inspector.get_indexes("inbound_messages")
    index_names = {idx["name"] for idx in indexes}
    assert "ux_inbound_dedup_channel_bot_update" in index_names, (
        f"Expected partial unique index 'ux_inbound_dedup_channel_bot_update' "
        f"in SQLite schema. Found: {index_names}"
    )
    # Also verify it covers the expected columns.
    target = next(
        idx for idx in indexes
        if idx["name"] == "ux_inbound_dedup_channel_bot_update"
    )
    assert set(target["column_names"]) == {"channel_type", "bot_key", "external_update_id"}
    assert target["unique"]  # SQLite Inspector returns 1 (int), not bool True
