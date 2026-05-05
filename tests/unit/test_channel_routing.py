"""Tests для services.channel_routing.resolve_outbox_routings (10.6).

Pre-existing producers (housewife_reminder, housewife_onboarding,
onboarding_aha, proactive_events) раньше hardcoded
`channel_type="telegram"`. Теперь они вызывают
`resolve_outbox_routings(session, tenant, user)` которая решает все
доступные channels на основе available account_id'ов.

**Dual delivery (Boris directive 2026-05-05):** если у юзера оба
TG+MAX account'а — функция возвращает list из двух OutboxRouting
(один TG, один MAX). Producers iterate и создают outbox row на
каждый channel — нотификация приходит и в TG и в МАКС.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.services.channel_routing import (
    OutboxRouting,
    resolve_outbox_routing,
    resolve_outbox_routings,
)


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


# ---------------------------------------------------------------------------
# resolve_outbox_routings (plural) — dual-delivery semantics
# ---------------------------------------------------------------------------


def test_routings_user_with_only_telegram(session):
    tenant, user = _add_user(session, telegram_account_id="111", max_account_id=None)
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert routings == [OutboxRouting(channel="telegram", chat_id="111")]


def test_routings_user_with_only_max(session):
    tenant, user = _add_user(
        session, telegram_account_id=None,
        max_account_id="222", max_chat_id="999",
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert routings == [OutboxRouting(channel="max", chat_id="999")]


def test_routings_user_with_both_returns_both_dual_delivery(session):
    """Boris directive: если оба channel'а — нотифицируем в оба."""
    tenant, user = _add_user(
        session, telegram_account_id="111",
        max_account_id="222", max_chat_id="999",
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert len(routings) == 2
    channels = [r.channel for r in routings]
    assert set(channels) == {"telegram", "max"}
    assert channels[0] == "telegram"  # TG first by default order


def test_routings_preferred_max_orders_max_first(session):
    """preferred_channel влияет ТОЛЬКО на порядок, оба остаются."""
    tenant, user = _add_user(
        session, telegram_account_id="111",
        max_account_id="222", max_chat_id="999",
        preferred_channel="max",
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert len(routings) == 2
    assert routings[0].channel == "max"   # primary first
    assert routings[1].channel == "telegram"


def test_routings_user_without_any_account_returns_empty(session):
    tenant, user = _add_user(
        session, telegram_account_id=None, max_account_id=None,
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert routings == []


def test_routings_no_user_returns_empty(session):
    assert resolve_outbox_routings(session, tenant=None, user=None) == []


def test_routings_max_uses_chat_id_not_account_id(session):
    tenant, user = _add_user(
        session, telegram_account_id=None,
        max_account_id="40921122",
        max_chat_id="320955459",
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    assert routings[0].channel == "max"
    assert routings[0].chat_id == "320955459"  # chat_id, not account_id


def test_routings_no_tenant_defaults_to_tg_first(session):
    user = User(id="u1", tenant_id="t_orphan",
                telegram_account_id="111", max_account_id="222",
                max_chat_id="999")
    session.add(Tenant(id="t_orphan", name="X"))
    session.add(user)
    session.commit()
    routings = resolve_outbox_routings(session, tenant=None, user=user)
    assert len(routings) == 2
    assert routings[0].channel == "telegram"


# ---------------------------------------------------------------------------
# resolve_outbox_routing (singular, backward-compat) — first routing only
# ---------------------------------------------------------------------------


def test_singular_returns_first_of_dual(session):
    tenant, user = _add_user(
        session, telegram_account_id="111",
        max_account_id="222", max_chat_id="999",
    )
    # Singular shim: returns ONLY first routing (loses second)
    routing = resolve_outbox_routing(session, tenant=tenant, user=user)
    assert routing is not None
    assert routing.channel == "telegram"


def test_singular_returns_none_when_no_accounts(session):
    tenant, user = _add_user(
        session, telegram_account_id=None, max_account_id=None,
    )
    assert resolve_outbox_routing(session, tenant=tenant, user=user) is None
