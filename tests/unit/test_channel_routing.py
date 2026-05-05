"""Tests для services.channel_routing.resolve_outbox_routing (10.6).

Pre-existing producers — `housewife_reminder_worker`,
`housewife_onboarding_worker`, `onboarding_aha_worker`,
`proactive_events` — раньше hardcoded `channel_type="telegram"`.
Теперь они вызывают `resolve_outbox_routing(session, tenant, user)`
которая решает channel + chat_id на основе:
- ``tenant.preferred_channel`` (если задан)
- fallback: telegram-first → max-fallback
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.services.channel_routing import OutboxRouting, resolve_outbox_routing


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    try:
        yield sess
    finally:
        sess.close()


def _add_user(session, **kw):
    """Helper: создать tenant + user с заданными account_id."""
    tenant_id = kw.pop("tenant_id", "t1")
    preferred = kw.pop("preferred_channel", None)
    if not session.get(Tenant, tenant_id):
        session.add(Tenant(id=tenant_id, name="T", preferred_channel=preferred))
    user = User(id=f"user_{tenant_id}", tenant_id=tenant_id, **kw)
    session.add(user)
    session.commit()
    return session.get(Tenant, tenant_id), user


def test_user_with_only_telegram_returns_telegram(session):
    tenant, user = _add_user(session, telegram_account_id="111", max_account_id=None)
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing == OutboxRouting(channel="telegram", chat_id="111")


def test_user_with_only_max_returns_max(session):
    tenant, user = _add_user(
        session, telegram_account_id=None,
        max_account_id="222", max_chat_id="999",
    )
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing == OutboxRouting(channel="max", chat_id="999")


def test_user_with_both_default_prefers_telegram(session):
    """Без preferred_channel default = TG-first (legacy behavior)."""
    tenant, user = _add_user(
        session, telegram_account_id="111",
        max_account_id="222", max_chat_id="999",
    )
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing.channel == "telegram"
    assert routing.chat_id == "111"


def test_preferred_max_routes_to_max_when_available(session):
    tenant, user = _add_user(
        session, telegram_account_id="111",
        max_account_id="222", max_chat_id="999",
        preferred_channel="max",
    )
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing.channel == "max"
    assert routing.chat_id == "999"


def test_preferred_max_falls_back_to_tg_if_no_max_account(session):
    """Если preferred=max но юзер не linked в MAX → TG fallback."""
    tenant, user = _add_user(
        session, telegram_account_id="111", max_account_id=None,
        preferred_channel="max",
    )
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing.channel == "telegram"


def test_preferred_telegram_explicit(session):
    tenant, user = _add_user(
        session, telegram_account_id="111",
        max_account_id="222", max_chat_id="999",
        preferred_channel="telegram",
    )
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing.channel == "telegram"


def test_user_without_any_account_returns_none(session):
    tenant, user = _add_user(
        session, telegram_account_id=None, max_account_id=None,
    )
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing is None


def test_no_tenant_passed_defaults_to_tg_first(session):
    """Если tenant=None — нет preferred_channel context'а, default TG-first."""
    sess = session
    user = User(id="u1", tenant_id="t_orphan",
                telegram_account_id="111", max_account_id="222",
                max_chat_id="999")
    sess.add(Tenant(id="t_orphan", name="X"))
    sess.add(user)
    sess.commit()
    routing = resolve_outbox_routing(sess, tenant=None, user=user)
    assert routing.channel == "telegram"


def test_no_user_returns_none(session):
    routing = resolve_outbox_routing(session, tenant=None, user=None)
    assert routing is None


def test_max_chat_id_used_not_account_id(session):
    """Important: для MAX используем max_chat_id (не max_account_id) —
    chat_id это recipient в `/messages?chat_id=` query."""
    tenant, user = _add_user(
        session, telegram_account_id=None,
        max_account_id="40921122",  # account_id ≠ chat_id
        max_chat_id="320955459",     # это реальный recipient
    )
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing.channel == "max"
    assert routing.chat_id == "320955459"  # not "40921122"
