"""Phase 4+10 — ensure_max_user_bundle tests.

Lock-in:
- New user → tenant + user + workspace + assistant created
- Returning user → no duplicate, max_chat_id updated если меняется
- Existing TG-tenant + same User row should NOT auto-merge (Boris's design:
  только explicit channel linking через mini-app)
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Assistant, Tenant, User, Workspace
from sreda.services.onboarding import (
    MaxOnboardingResult,
    ensure_max_user_bundle,
    find_user_by_max_account_id,
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


def test_new_max_user_creates_full_bundle(session):
    result = ensure_max_user_bundle(
        session, max_account_id="40921122", max_chat_id="320955459",
        display_name="Борис",
    )
    assert result.is_new_user is True
    assert result.max_account_id == "40921122"
    assert result.max_chat_id == "320955459"
    assert result.tenant_id == "tenant_max_40921122"
    assert result.user_id == "user_max_40921122"
    assert result.workspace_id == "workspace_max_40921122"
    assert result.assistant_id == "assistant_max_40921122"

    user = session.get(User, result.user_id)
    assert user.max_account_id == "40921122"
    assert user.max_chat_id == "320955459"
    assert user.tenant_id == "tenant_max_40921122"

    assert session.get(Tenant, "tenant_max_40921122") is not None
    assert session.get(Workspace, "workspace_max_40921122") is not None
    assert session.get(Assistant, "assistant_max_40921122") is not None


def test_returning_max_user_no_duplicate(session):
    first = ensure_max_user_bundle(
        session, max_account_id="40921122", max_chat_id="320955459",
        display_name="Борис",
    )
    second = ensure_max_user_bundle(
        session, max_account_id="40921122", max_chat_id="320955459",
    )
    assert first.user_id == second.user_id
    assert second.is_new_user is False
    assert second.tenant_id == first.tenant_id

    # Только одна User row
    users = session.query(User).filter(User.max_account_id == "40921122").all()
    assert len(users) == 1


def test_returning_max_user_updates_chat_id_if_changed(session):
    """Если у юзера ранее не был max_chat_id (миграция edge case),
    второй вызов с chat_id обновляет."""
    # Manually create User without chat_id
    session.add(Tenant(id="tenant_max_42", name="X"))
    session.add(User(id="user_max_42", tenant_id="tenant_max_42", max_account_id="42"))
    session.commit()

    result = ensure_max_user_bundle(
        session, max_account_id="42", max_chat_id="999_new_chat",
    )
    assert result.is_new_user is False

    user = session.get(User, "user_max_42")
    assert user.max_chat_id == "999_new_chat"


def test_find_user_by_max_account_id(session):
    ensure_max_user_bundle(
        session, max_account_id="111", display_name="X",
    )

    found = find_user_by_max_account_id(session, "111")
    assert found is not None
    assert found.max_account_id == "111"

    assert find_user_by_max_account_id(session, "9999") is None
    assert find_user_by_max_account_id(session, "") is None
    assert find_user_by_max_account_id(session, None) is None


def test_empty_max_account_id_returns_empty_result(session):
    result = ensure_max_user_bundle(session, max_account_id="")
    assert result.is_new_user is False
    assert result.tenant_id is None
    assert result.user_id is None


def test_max_user_does_NOT_merge_with_existing_tg_tenant(session):
    """Boris's design: автоmerge запрещён. Юзер с TG-аккаунтом и юзер
    с MAX-аккаунтом — отдельные tenants до явного channel linking."""
    # Manually create TG tenant
    session.add(Tenant(id="tenant_tg_100", name="TG-Boris"))
    session.add(User(id="user_tg_100", tenant_id="tenant_tg_100", telegram_account_id="100"))
    session.commit()

    # Now Boris пишет ВПЕРВЫЕ в MAX (другой user_id)
    max_result = ensure_max_user_bundle(
        session, max_account_id="40921122",
    )

    # Должен быть отдельный tenant
    assert max_result.tenant_id == "tenant_max_40921122"
    assert max_result.tenant_id != "tenant_tg_100"

    # Existing TG tenant не сломан
    tg_user = session.get(User, "user_tg_100")
    assert tg_user.tenant_id == "tenant_tg_100"
    assert tg_user.max_account_id is None  # not auto-merged
