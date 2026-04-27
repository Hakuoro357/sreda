"""Тесты для колонки address_form (backlog: возврат формы «вы»).

С 2026-04-27 (вечер) функция «вы»-обращения отключена в продукте,
но колонка `tenant_user_profiles.address_form` и поддержка в
репозитории остаются для будущей реализации (см.
`docs/tomorrow-plan.md` пункт 8).

Эти тесты гарантируют что:
  * колонка добавлена в схему и round-trip'ит значения «ty»/«vy»
  * `UserProfileRepository.update_profile(address_form=...)` пишет
    значение и валидирует enum

Удалены: тесты на helper'ы `build_name_question_message` /
`build_address_form_question_message` (функции удалены), тесты на
`pick_ack(address_form=...)` (откачено до базовой версии),
проверки `[ПРОФИЛЬ]` блока про «ты/вы» (удалён из system-prompt).
"""

from __future__ import annotations

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.user_profile import TenantUserProfile


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    return Session()


# --------------------- repository --------------------------

def test_update_profile_address_form():
    """Repo пишет address_form (для возврата фичи из backlog)."""
    from sreda.db.repositories.user_profile import UserProfileRepository

    session = _make_session()
    session.add(Tenant(id="t1", name="T"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    session.commit()

    repo = UserProfileRepository(session)
    profile = repo.update_profile("t1", "u1", address_form="vy")
    session.commit()

    assert profile.address_form == "vy"

    # Перевыбор работает.
    profile = repo.update_profile("t1", "u1", address_form="ty")
    session.commit()
    assert profile.address_form == "ty"


def test_update_profile_address_form_validates():
    from sreda.db.repositories.user_profile import UserProfileRepository

    session = _make_session()
    session.add(Tenant(id="t1", name="T"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    session.commit()

    repo = UserProfileRepository(session)
    with pytest.raises(ValueError, match="address_form"):
        repo.update_profile("t1", "u1", address_form="нечто")


# --------------------- model round-trip ---------------

def test_address_form_column_round_trip():
    """Колонка хранит значение и читается обратно."""
    session = _make_session()
    session.add(Tenant(id="t1", name="T"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    session.add(TenantUserProfile(
        id="tup_1", tenant_id="t1", user_id="u1",
        display_name="Борис", address_form="vy",
        created_at=now, updated_at=now,
    ))
    session.commit()

    fresh = session.query(TenantUserProfile).filter_by(id="tup_1").one()
    assert fresh.address_form == "vy"
    assert fresh.display_name == "Борис"
