"""Phase 7+10 — channel_linking service tests.

Lock-in tests для security-critical paths:
- token generation (256-bit, hashed at rest)
- atomic consume (single-use, race-safe)
- TTL expiration
- collision detection (existing tenant в target channel)
- rate-limit (5 starts per 30 min)
- cleanup (mandatory expired-token DELETE)

Не пытаемся reverse-engineer initData validation тут — отдельный модуль
test_max_auth.py покрыт частично, остальное — live test в Phase 11.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.channel_linking import ChannelLinkToken
from sreda.db.models.core import Tenant, User
from sreda.services.tg_account_hash import hash_tg_account
from sreda.services.channel_linking import (
    ChannelLinkRateLimitedError,
    RATE_LIMIT_MAX,
    cleanup_expired_tokens,
    consume_link,
    lookup_token,
    start_link,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T1"))
    sess.add(Tenant(id="t2", name="T2"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.add(User(id="u_max_other", tenant_id="t2", max_account_id="999"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# start_link
# ---------------------------------------------------------------------------


def test_start_link_creates_opaque_token_with_hash(session):
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )

    assert result.target_channel == "max"
    assert result.deep_link.startswith("https://max.ru/id320700072280_bot?startapp=lnk_")
    assert len(result.raw_token) >= 32

    # Token хранится hashed
    row = session.get(ChannelLinkToken, result.id)
    assert row is not None
    assert len(row.token_hash) == 64  # SHA-256 hex
    assert row.token_hash != result.raw_token  # raw НЕ stored

    # Не expired (SQLite strips tzinfo на round-trip — coerce для сравнения)
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    assert expires > datetime.now(timezone.utc)
    assert row.used_at is None


def test_start_link_max_to_telegram_direction(session):
    result = start_link(
        session, tenant_id="t1", source_channel="max", source_user_id="u1",
        tg_bot_username="sreda_test_bot", tg_miniapp_shortname="sreda_app",
    )
    assert result.target_channel == "telegram"
    assert result.deep_link.startswith(
        "https://t.me/sreda_test_bot/sreda_app?startapp=lnk_"
    )


def test_start_link_unknown_source_raises(session):
    with pytest.raises(ValueError, match="unknown source_channel"):
        start_link(
            session, tenant_id="t1", source_channel="discord", source_user_id="u1",
        )


def test_start_link_rate_limited_after_max_attempts(session):
    for _ in range(RATE_LIMIT_MAX):
        start_link(
            session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
        )

    with pytest.raises(ChannelLinkRateLimitedError):
        start_link(
            session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
        )


# ---------------------------------------------------------------------------
# lookup_token
# ---------------------------------------------------------------------------


def test_lookup_returns_active_token(session):
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    found = lookup_token(session, result.raw_token)
    assert found is not None
    assert found.id == result.id


def test_lookup_returns_none_for_unknown(session):
    assert lookup_token(session, "garbage") is None


def test_lookup_returns_none_for_expired(session):
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    row = session.get(ChannelLinkToken, result.id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()
    assert lookup_token(session, result.raw_token) is None


def test_lookup_returns_none_for_used(session):
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    row = session.get(ChannelLinkToken, result.id)
    row.used_at = datetime.now(timezone.utc)
    session.commit()
    assert lookup_token(session, result.raw_token) is None


# ---------------------------------------------------------------------------
# consume_link
# ---------------------------------------------------------------------------


def test_consume_link_success_links_account(session):
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    outcome = consume_link(
        session,
        raw_token=result.raw_token,
        target_channel="max",
        target_account_id="555",
        target_chat_id="chat_555",
    )
    assert outcome.success is True
    assert outcome.tenant_id == "t1"
    assert outcome.idempotent is False

    # User у tenant_t1 должен теперь иметь max_account_id=555
    user = session.get(User, "u1")
    assert user.max_account_id == "555"

    # Token marked used
    row = session.get(ChannelLinkToken, result.id)
    assert row.used_at is not None


def test_consume_link_replay_fails(session):
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    consume_link(
        session, raw_token=result.raw_token,
        target_channel="max", target_account_id="555", target_chat_id="chat_555",
    )
    # Second consume same token
    outcome2 = consume_link(
        session, raw_token=result.raw_token,
        target_channel="max", target_account_id="666", target_chat_id="chat_666",
    )
    assert outcome2.success is False
    assert outcome2.error == "not_found_or_expired"


def test_consume_link_expired_fails(session):
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    row = session.get(ChannelLinkToken, result.id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()

    outcome = consume_link(
        session, raw_token=result.raw_token, target_channel="max",
        target_account_id="555", target_chat_id="chat_555",
    )
    assert outcome.success is False
    assert outcome.error == "not_found_or_expired"


def test_consume_link_unknown_token(session):
    outcome = consume_link(
        session, raw_token="A" * 32, target_channel="max",
        target_account_id="555", target_chat_id="chat_555",
    )
    assert outcome.success is False
    assert outcome.error == "not_found_or_expired"


def test_consume_link_collision_returns_error(session):
    """Если target account_id уже принадлежит ДРУГОМУ tenant'у — abort."""
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    # max_account_id=999 уже у t2 (см. fixture)
    outcome = consume_link(
        session, raw_token=result.raw_token, target_channel="max",
        target_account_id="999", target_chat_id="chat_999",
    )
    assert outcome.success is False
    assert outcome.error == "account_already_registered_separately"

    session.expire_all()
    other = session.get(User, "u_max_other")
    assert other.tenant_id == "t2"  # не мутировался


def test_consume_link_wrong_target_channel(session):
    """Token создан с target=max, но consume вызван с target=telegram → reject."""
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    outcome = consume_link(
        session, raw_token=result.raw_token,
        target_channel="telegram",  # WRONG — token want max
        target_account_id="555",
    )
    assert outcome.success is False
    assert outcome.error == "not_found_or_expired"


def test_consume_link_idempotent_same_target(session):
    first = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    consume_link(
        session, raw_token=first.raw_token, target_channel="max",
        target_account_id="555", target_chat_id="chat_555",
    )

    second = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )
    outcome = consume_link(
        session, raw_token=second.raw_token, target_channel="max",
        target_account_id="555", target_chat_id="chat_555",
    )

    assert outcome.success is True
    assert outcome.idempotent is True


def test_consume_link_already_linked_different_target_error(session):
    source_user = session.get(User, "u1")
    source_user.max_account_id = "222"
    session.commit()
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )

    outcome = consume_link(
        session, raw_token=result.raw_token, target_channel="max",
        target_account_id="333", target_chat_id="chat_333",
    )

    assert outcome.success is False
    assert outcome.error == "already_linked_other_account"


def test_consume_link_max_missing_chat_id(session):
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )

    outcome = consume_link(
        session, raw_token=result.raw_token, target_channel="max",
        target_account_id="555",
    )

    assert outcome.success is False
    assert outcome.error == "missing_chat_id"


def test_consume_link_max_attaches_chat_id(session):
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )

    outcome = consume_link(
        session, raw_token=result.raw_token, target_channel="max",
        target_account_id="555", target_chat_id="chat_555",
    )

    assert outcome.success is True
    assert session.get(User, "u1").max_chat_id == "chat_555"


def test_consume_link_tg_target_uses_hash(session):
    source = User(id="u_max_source", tenant_id="t1", max_account_id="333")
    hashed_other = User(
        id="u_tg_hashed_other",
        tenant_id="t2",
        tg_account_hash=hash_tg_account("777"),
    )
    session.add_all([source, hashed_other])
    session.commit()
    result = start_link(
        session, tenant_id="t1", source_channel="max",
        source_user_id="u_max_source", tg_bot_username="sreda_test_bot",
        tg_miniapp_shortname="sreda_app",
    )

    outcome = consume_link(
        session, raw_token=result.raw_token, target_channel="telegram",
        target_account_id="777",
    )

    assert outcome.success is False
    assert outcome.error == "account_already_registered_separately"


def test_consume_link_invalid_token_format(session):
    with pytest.raises(ValueError, match="invalid token format"):
        consume_link(
            session, raw_token="garbage", target_channel="max",
            target_account_id="555", target_chat_id="chat_555",
        )


def test_consume_link_same_tenant_same_user_idempotent(session):
    source_user = session.get(User, "u1")
    source_user.max_account_id = "555"
    session.commit()
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )

    outcome = consume_link(
        session, raw_token=result.raw_token, target_channel="max",
        target_account_id="555", target_chat_id="chat_555",
    )

    assert outcome.success is True
    assert outcome.idempotent is True


def test_consume_link_same_tenant_other_user_blocked(session):
    session.add(User(id="u_same_tenant_other", tenant_id="t1", max_account_id="555"))
    session.commit()
    result = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )

    outcome = consume_link(
        session, raw_token=result.raw_token, target_channel="max",
        target_account_id="555", target_chat_id="chat_555",
    )

    assert outcome.success is False
    assert outcome.error == "account_belongs_to_other_family_member"


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def test_cleanup_deletes_expired_tokens(session):
    # Свежий — should survive
    fresh = start_link(
        session, tenant_id="t1", source_channel="telegram", source_user_id="u1",
    )

    # Старый (expired more than 1 day ago) — should be deleted
    stale = ChannelLinkToken(
        id="link_stale",
        tenant_id="t1",
        source_channel="max",
        target_channel="telegram",
        token_hash="dead" * 16,
        expires_at=datetime.now(timezone.utc) - timedelta(days=2),
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    session.add(stale)
    session.commit()

    deleted = cleanup_expired_tokens(session)
    assert deleted == 1

    # Fresh всё ещё на месте
    assert session.get(ChannelLinkToken, fresh.id) is not None
    assert session.get(ChannelLinkToken, "link_stale") is None
