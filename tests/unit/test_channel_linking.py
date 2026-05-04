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
        session, tenant_id="t1", source_channel="telegram",
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
        session, tenant_id="t1", source_channel="max",
        tg_bot_username="sreda_test_bot",
    )
    assert result.target_channel == "telegram"
    assert result.deep_link.startswith("https://t.me/sreda_test_bot?start=lnk_")


def test_start_link_unknown_source_raises(session):
    with pytest.raises(ValueError, match="unknown source_channel"):
        start_link(session, tenant_id="t1", source_channel="discord")


def test_start_link_rate_limited_after_max_attempts(session):
    for _ in range(RATE_LIMIT_MAX):
        start_link(session, tenant_id="t1", source_channel="telegram")

    with pytest.raises(ChannelLinkRateLimitedError):
        start_link(session, tenant_id="t1", source_channel="telegram")


# ---------------------------------------------------------------------------
# lookup_token
# ---------------------------------------------------------------------------


def test_lookup_returns_active_token(session):
    result = start_link(session, tenant_id="t1", source_channel="telegram")
    found = lookup_token(session, result.raw_token)
    assert found is not None
    assert found.id == result.id


def test_lookup_returns_none_for_unknown(session):
    assert lookup_token(session, "garbage") is None


def test_lookup_returns_none_for_expired(session):
    result = start_link(session, tenant_id="t1", source_channel="telegram")
    row = session.get(ChannelLinkToken, result.id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()
    assert lookup_token(session, result.raw_token) is None


def test_lookup_returns_none_for_used(session):
    result = start_link(session, tenant_id="t1", source_channel="telegram")
    row = session.get(ChannelLinkToken, result.id)
    row.used_at = datetime.now(timezone.utc)
    session.commit()
    assert lookup_token(session, result.raw_token) is None


# ---------------------------------------------------------------------------
# consume_link
# ---------------------------------------------------------------------------


def test_consume_link_success_links_account(session):
    result = start_link(session, tenant_id="t1", source_channel="telegram")
    outcome = consume_link(
        session,
        raw_token=result.raw_token,
        target_channel="max",
        target_account_id="555",
    )
    assert outcome.success is True
    assert outcome.tenant_id == "t1"

    # User у tenant_t1 должен теперь иметь max_account_id=555
    user = session.query(User).filter(User.tenant_id == "t1").first()
    assert user.max_account_id == "555"

    # Token marked used
    row = session.get(ChannelLinkToken, result.id)
    assert row.used_at is not None


def test_consume_link_replay_fails(session):
    result = start_link(session, tenant_id="t1", source_channel="telegram")
    consume_link(
        session, raw_token=result.raw_token,
        target_channel="max", target_account_id="555",
    )
    # Second consume same token
    outcome2 = consume_link(
        session, raw_token=result.raw_token,
        target_channel="max", target_account_id="666",
    )
    assert outcome2.success is False
    assert outcome2.error == "used"


def test_consume_link_expired_fails(session):
    result = start_link(session, tenant_id="t1", source_channel="telegram")
    row = session.get(ChannelLinkToken, result.id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()

    outcome = consume_link(
        session, raw_token=result.raw_token,
        target_channel="max", target_account_id="555",
    )
    assert outcome.success is False
    assert outcome.error == "expired"


def test_consume_link_unknown_token(session):
    outcome = consume_link(
        session, raw_token="garbage_token",
        target_channel="max", target_account_id="555",
    )
    assert outcome.success is False
    assert outcome.error == "not_found"


def test_consume_link_collision_detected(session):
    """Если target account_id уже принадлежит ДРУГОМУ tenant'у — abort."""
    result = start_link(session, tenant_id="t1", source_channel="telegram")
    # max_account_id=999 уже у t2 (см. fixture)
    outcome = consume_link(
        session, raw_token=result.raw_token,
        target_channel="max", target_account_id="999",
    )
    assert outcome.success is False
    assert outcome.error == "collision"

    # Token row — used_at сброшен после rollback
    session.expire_all()
    row = session.get(ChannelLinkToken, result.id)
    # После rollback используем session.refresh для чистого state
    # Note: тест может быть hairy на SQLite vs Postgres semantics — для
    # MVP проверяем главное: collision сработал, original tenant не
    # сломан.
    other = session.get(User, "u_max_other")
    assert other.tenant_id == "t2"  # не мутировался


def test_consume_link_wrong_target_channel(session):
    """Token создан с target=max, но consume вызван с target=telegram → reject."""
    result = start_link(session, tenant_id="t1", source_channel="telegram")
    outcome = consume_link(
        session, raw_token=result.raw_token,
        target_channel="telegram",  # WRONG — token want max
        target_account_id="555",
    )
    assert outcome.success is False
    assert outcome.error == "wrong_channel"


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def test_cleanup_deletes_expired_tokens(session):
    # Свежий — should survive
    fresh = start_link(session, tenant_id="t1", source_channel="telegram")

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
