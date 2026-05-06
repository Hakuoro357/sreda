"""Phase 8 — integration tests for channel-linking MVP.

Full end-to-end flows using real SQLite session:
  * TG → MAX happy path + audit log + tenant isolation
  * Collision blocks with clear error
  * Token replay blocked
  * Token expired
  * Idempotent retry same target
  * Already-linked other account blocked
  * TG target uses hash for collision
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.audit import AuditLog
from sreda.db.models.channel_linking import ChannelLinkToken
from sreda.db.models.core import Tenant, User
from sreda.services.channel_linking import consume_link, start_link


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    try:
        yield sess
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# test_full_tg_to_max_happy_path
# ---------------------------------------------------------------------------


def test_full_tg_to_max_happy_path(session):
    """Full TG → MAX linking: success, DB mutation, audit log, tenant isolation."""
    # --- setup ---
    session.add(Tenant(id="tenant_tg", name="TG Tenant"))
    session.add(User(id="user_tg", tenant_id="tenant_tg", telegram_account_id="40921122"))
    # Unrelated tenant for isolation check
    session.add(Tenant(id="tenant_unrelated", name="Unrelated"))
    session.add(User(id="user_unrelated", tenant_id="tenant_unrelated", telegram_account_id="8888"))
    session.commit()

    # --- act: start_link ---
    start = start_link(
        session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    assert start.target_channel == "max"

    # --- act: consume_link ---
    outcome = consume_link(
        session,
        raw_token=start.raw_token,
        target_channel="max",
        target_account_id="9999",
        target_chat_id="chat_9999",
    )

    # --- assert: outcome ---
    assert outcome.success is True
    assert outcome.tenant_id == "tenant_tg"
    assert outcome.idempotent is False

    # --- assert: user mutated ---
    user_tg = session.get(User, "user_tg")
    assert user_tg.max_account_id == "9999"
    assert user_tg.max_chat_id == "chat_9999"

    # --- assert: audit log ---
    logs = (
        session.query(AuditLog)
        .filter(AuditLog.action == "channel_link.attached")
        .all()
    )
    assert len(logs) == 1
    log = logs[0]
    assert log.actor_type == "user"
    assert log.actor_id == "user_tg"
    assert log.resource_type == "tenant"
    assert log.resource_id == "tenant_tg"

    import json

    meta = json.loads(log.metadata_json)
    assert meta["target_channel"] == "max"
    assert meta["target_account_id_present"] is True
    assert meta["target_chat_id_present"] is True
    assert meta["token_id"] == start.id

    # --- assert: tenant_unrelated untouched ---
    unrelated_user = session.get(User, "user_unrelated")
    assert unrelated_user.telegram_account_id == "8888"
    assert unrelated_user.max_account_id is None
    assert unrelated_user.max_chat_id is None

    # Verify no audit log rows for unrelated tenant
    unrelated_logs = (
        session.query(AuditLog)
        .filter(AuditLog.resource_id == "tenant_unrelated")
        .all()
    )
    assert len(unrelated_logs) == 0


# ---------------------------------------------------------------------------
# test_collision_blocks_with_clear_error
# ---------------------------------------------------------------------------


def test_collision_blocks_with_clear_error(session):
    """Cross-tenant collision: error returned, no mutation, no destructive merge."""
    # --- setup ---
    session.add(Tenant(id="tenant_a", name="Tenant A"))
    session.add(User(id="user_a", tenant_id="tenant_a", telegram_account_id="100"))
    session.add(Tenant(id="tenant_b", name="Tenant B"))
    session.add(User(id="user_b", tenant_id="tenant_b", max_account_id="200"))
    session.commit()

    # --- act: start_link from tenant_a ---
    start = start_link(
        session,
        tenant_id="tenant_a",
        source_channel="telegram",
        source_user_id="user_a",
    )

    # --- act: consume with colliding account_id ---
    outcome = consume_link(
        session,
        raw_token=start.raw_token,
        target_channel="max",
        target_account_id="200",
        target_chat_id="chat_200",
    )

    # --- assert: collision error ---
    assert outcome.success is False
    assert outcome.error == "account_already_registered_separately"

    # --- assert: user_a NOT mutated ---
    session.expire_all()
    user_a = session.get(User, "user_a")
    assert user_a.max_account_id is None

    # --- assert: user_b (tenant_b) NOT deleted — no destructive merge in MVP ---
    user_b = session.get(User, "user_b")
    assert user_b is not None
    assert user_b.max_account_id == "200"
    assert user_b.tenant_id == "tenant_b"

    # --- assert: no audit log for collision ---
    logs = (
        session.query(AuditLog)
        .filter(AuditLog.action == "channel_link.attached")
        .all()
    )
    assert len(logs) == 0


# ---------------------------------------------------------------------------
# test_token_replay_blocked
# ---------------------------------------------------------------------------


def test_token_replay_blocked(session):
    """Second consume with same raw_token returns not_found_or_expired."""
    # --- setup ---
    session.add(Tenant(id="tenant_tg", name="TG Tenant"))
    session.add(User(id="user_tg", tenant_id="tenant_tg", telegram_account_id="100"))
    session.commit()

    # --- act: first consume (success) ---
    start = start_link(
        session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    outcome1 = consume_link(
        session,
        raw_token=start.raw_token,
        target_channel="max",
        target_account_id="300",
        target_chat_id="chat_300",
    )
    assert outcome1.success is True

    # --- act: second consume with same raw_token ---
    outcome2 = consume_link(
        session,
        raw_token=start.raw_token,
        target_channel="max",
        target_account_id="300",
        target_chat_id="chat_300",
    )

    # --- assert: replay blocked ---
    assert outcome2.success is False
    assert outcome2.error == "not_found_or_expired"

    # --- assert: user unchanged from first call ---
    session.expire_all()
    user_tg = session.get(User, "user_tg")
    assert user_tg.max_account_id == "300"
    assert user_tg.max_chat_id == "chat_300"


# ---------------------------------------------------------------------------
# test_token_expired
# ---------------------------------------------------------------------------


def test_token_expired(session):
    """Expired token → not_found_or_expired, no audit log."""
    # --- setup ---
    session.add(Tenant(id="tenant_tg", name="TG Tenant"))
    session.add(User(id="user_tg", tenant_id="tenant_tg", telegram_account_id="100"))
    session.commit()

    # --- act: start_link then manually expire ---
    start = start_link(
        session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    row = session.get(ChannelLinkToken, start.id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()

    # --- act: consume expired token ---
    outcome = consume_link(
        session,
        raw_token=start.raw_token,
        target_channel="max",
        target_account_id="500",
        target_chat_id="chat_500",
    )

    # --- assert: expired → error ---
    assert outcome.success is False
    assert outcome.error == "not_found_or_expired"

    # --- assert: no audit log ---
    logs = (
        session.query(AuditLog)
        .filter(AuditLog.action == "channel_link.attached")
        .all()
    )
    assert len(logs) == 0


# ---------------------------------------------------------------------------
# test_idempotent_retry_same_target
# ---------------------------------------------------------------------------


def test_idempotent_retry_same_target(session):
    """Same target on fresh token → idempotent success."""
    # --- setup ---
    session.add(Tenant(id="tenant_tg", name="TG Tenant"))
    session.add(User(id="user_tg", tenant_id="tenant_tg", telegram_account_id="100"))
    session.commit()

    # --- act: first link (success) ---
    start1 = start_link(
        session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    outcome1 = consume_link(
        session,
        raw_token=start1.raw_token,
        target_channel="max",
        target_account_id="300",
        target_chat_id="chat_300",
    )
    assert outcome1.success is True
    assert outcome1.idempotent is False

    # --- act: fresh start_link, same target ---
    start2 = start_link(
        session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    outcome2 = consume_link(
        session,
        raw_token=start2.raw_token,
        target_channel="max",
        target_account_id="300",
        target_chat_id="chat_300",
    )

    # --- assert: idempotent success ---
    assert outcome2.success is True
    assert outcome2.idempotent is True

    # --- assert: user unchanged ---
    session.expire_all()
    user_tg = session.get(User, "user_tg")
    assert user_tg.max_account_id == "300"
    assert user_tg.max_chat_id == "chat_300"


# ---------------------------------------------------------------------------
# test_already_linked_other_account_blocked
# ---------------------------------------------------------------------------


def test_already_linked_other_account_blocked(session):
    """Trying to link a DIFFERENT account_id when already linked → blocked."""
    # --- setup ---
    session.add(Tenant(id="tenant_tg", name="TG Tenant"))
    session.add(User(id="user_tg", tenant_id="tenant_tg", telegram_account_id="100"))
    session.commit()

    # --- act: first link (account=400, success) ---
    start1 = start_link(
        session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    outcome1 = consume_link(
        session,
        raw_token=start1.raw_token,
        target_channel="max",
        target_account_id="400",
        target_chat_id="chat_400",
    )
    assert outcome1.success is True

    # --- act: fresh start, try DIFFERENT account_id (500) ---
    start2 = start_link(
        session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    outcome2 = consume_link(
        session,
        raw_token=start2.raw_token,
        target_channel="max",
        target_account_id="500",
        target_chat_id="chat_500",
    )

    # --- assert: blocked ---
    assert outcome2.success is False
    assert outcome2.error == "already_linked_other_account"

    # --- assert: user NOT overwritten ---
    session.expire_all()
    user_tg = session.get(User, "user_tg")
    assert user_tg.max_account_id == "400"
    assert user_tg.max_chat_id == "chat_400"


# ---------------------------------------------------------------------------
# test_tg_target_uses_hash_for_collision
# ---------------------------------------------------------------------------


def test_tg_target_uses_hash_for_collision(session):
    """TG target collision uses tg_account_hash, not raw column."""
    # --- setup ---
    # tenant_a: user has max only (source = max, target = telegram)
    session.add(Tenant(id="tenant_a", name="Tenant A"))
    session.add(User(id="user_a", tenant_id="tenant_a", max_account_id="111"))
    # tenant_b: user has telegram=200 ONLY via tg_account_hash
    from sreda.services.tg_account_hash import hash_tg_account

    session.add(Tenant(id="tenant_b", name="Tenant B"))
    session.add(
        User(
            id="user_b",
            tenant_id="tenant_b",
            tg_account_hash=hash_tg_account("200"),
        )
    )
    session.commit()

    # --- act: start_link from tenant_a (source=max, target=telegram) ---
    start = start_link(
        session,
        tenant_id="tenant_a",
        source_channel="max",
        source_user_id="user_a",
        tg_bot_username="sreda_test_bot",
        tg_miniapp_shortname="sreda_app",
    )
    assert start.target_channel == "telegram"

    # --- act: consume with colliding telegram account_id ---
    outcome = consume_link(
        session,
        raw_token=start.raw_token,
        target_channel="telegram",
        target_account_id="200",
    )

    # --- assert: collision via hash lookup ---
    assert outcome.success is False
    assert outcome.error == "account_already_registered_separately"

    # --- assert: neither user mutated ---
    session.expire_all()
    user_a = session.get(User, "user_a")
    assert user_a.telegram_account_id is None
    assert user_a.max_account_id == "111"

    user_b = session.get(User, "user_b")
    assert user_b.tg_account_hash == hash_tg_account("200")
    assert user_b.tenant_id == "tenant_b"