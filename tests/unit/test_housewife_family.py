"""Unit tests for HousewifeFamilyService."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife import FamilyMember
from sreda.services.housewife_family import HousewifeFamilyService


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.commit()
    yield sess
    sess.close()


# ---------------------------------------------------------------------------
# add_member
# ---------------------------------------------------------------------------


def test_add_member_persists(session):
    svc = HousewifeFamilyService(session)
    m = svc.add_member(
        tenant_id="t1", user_id="u1",
        name="Екатерина", role="spouse",
    )
    assert m.id.startswith("fm_")
    assert m.name == "Екатерина"
    assert m.role == "spouse"


def test_add_member_with_all_fields(session):
    svc = HousewifeFamilyService(session)
    m = svc.add_member(
        tenant_id="t1", user_id="u1",
        name="Маша", role="child",
        birth_year=2017, notes="аллергия на горчицу",
    )
    assert m.birth_year == 2017
    assert m.notes == "аллергия на горчицу"


def test_add_member_empty_name_rejects(session):
    svc = HousewifeFamilyService(session)
    with pytest.raises(ValueError, match="name"):
        svc.add_member(tenant_id="t1", user_id="u1", name="", role="child")


def test_add_member_unknown_role_rejects(session):
    svc = HousewifeFamilyService(session)
    with pytest.raises(ValueError, match="role"):
        svc.add_member(tenant_id="t1", user_id="u1", name="X", role="pet")


def test_add_member_implausible_birth_year_rejects(session):
    svc = HousewifeFamilyService(session)
    with pytest.raises(ValueError, match="birth_year"):
        svc.add_member(
            tenant_id="t1", user_id="u1", name="X", role="child",
            birth_year=1800,
        )


def test_add_member_name_is_encrypted_at_rest(session):
    svc = HousewifeFamilyService(session)
    svc.add_member(
        tenant_id="t1", user_id="u1",
        name="конфиденциальное имя", role="child",
    )
    raw = session.execute(text("SELECT name FROM family_members")).scalar()
    assert raw.startswith("v2:")
    assert "конфиденциальное" not in raw


# ---------------------------------------------------------------------------
# add_members_batch
# ---------------------------------------------------------------------------


def test_add_members_batch_persists_all_valid(session):
    svc = HousewifeFamilyService(session)
    created = svc.add_members_batch(
        tenant_id="t1", user_id="u1",
        members=[
            {"name": "Борис", "role": "self"},
            {"name": "Екатерина", "role": "spouse"},
            {"name": "Николай", "role": "child", "birth_year": 2015},
        ],
    )
    assert len(created) == 3
    names = {m.name for m in created}
    assert names == {"Борис", "Екатерина", "Николай"}


def test_add_members_batch_skips_invalid(session):
    svc = HousewifeFamilyService(session)
    created = svc.add_members_batch(
        tenant_id="t1", user_id="u1",
        members=[
            {"name": "OK", "role": "child"},
            {"name": "", "role": "child"},           # empty name
            {"name": "Bad", "role": "pet"},          # unknown role
            {"name": "Also OK", "role": "self"},
            "not a dict",
        ],
    )
    assert {m.name for m in created} == {"OK", "Also OK"}


# ---------------------------------------------------------------------------
# list_members / count_eaters
# ---------------------------------------------------------------------------


def test_list_members_empty_yields_empty(session):
    svc = HousewifeFamilyService(session)
    assert svc.list_members(tenant_id="t1", user_id="u1") == []


def test_list_members_ordered_by_role(session):
    svc = HousewifeFamilyService(session)
    svc.add_member(tenant_id="t1", user_id="u1", name="Boris", role="self")
    svc.add_member(tenant_id="t1", user_id="u1", name="Kate", role="spouse")
    svc.add_member(tenant_id="t1", user_id="u1", name="Nick", role="child")
    svc.add_member(tenant_id="t1", user_id="u1", name="Grandma", role="parent")

    names_by_role = {m.role: m.name for m in svc.list_members(tenant_id="t1", user_id="u1")}
    assert names_by_role == {
        "self": "Boris",
        "spouse": "Kate",
        "child": "Nick",
        "parent": "Grandma",
    }


def test_count_eaters_returns_1_for_empty_family(session):
    """No members recorded → fallback to 1 (the user alone) so shopping
    scaling doesn't go to zero."""
    svc = HousewifeFamilyService(session)
    assert svc.count_eaters(tenant_id="t1", user_id="u1") == 1


def test_count_eaters_totals_members(session):
    svc = HousewifeFamilyService(session)
    svc.add_members_batch(
        tenant_id="t1", user_id="u1",
        members=[
            {"name": "Борис", "role": "self"},
            {"name": "Жена", "role": "spouse"},
            {"name": "Сын", "role": "child"},
        ],
    )
    assert svc.count_eaters(tenant_id="t1", user_id="u1") == 3


def test_count_eaters_tenant_scoped(session):
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()

    svc = HousewifeFamilyService(session)
    svc.add_members_batch(
        tenant_id="t1", user_id="u1",
        members=[{"name": "A", "role": "self"}, {"name": "B", "role": "spouse"}],
    )
    # t2 has no members yet
    assert svc.count_eaters(tenant_id="t2", user_id="u2") == 1
    assert svc.count_eaters(tenant_id="t1", user_id="u1") == 2


# ---------------------------------------------------------------------------
# update_member / remove_member
# ---------------------------------------------------------------------------


def test_update_member_applies_fields(session):
    svc = HousewifeFamilyService(session)
    m = svc.add_member(tenant_id="t1", user_id="u1", name="Маша", role="child")

    updated = svc.update_member(
        tenant_id="t1", user_id="u1", member_id=m.id,
        birth_year=2017, notes="аллергия",
    )
    assert updated is not None
    assert updated.birth_year == 2017
    assert updated.notes == "аллергия"
    # Unchanged fields stay
    assert updated.name == "Маша"


def test_update_member_cross_tenant_returns_none(session):
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()

    svc = HousewifeFamilyService(session)
    m = svc.add_member(tenant_id="t1", user_id="u1", name="X", role="child")

    result = svc.update_member(
        tenant_id="t2", user_id="u2", member_id=m.id,
        name="Tampered",
    )
    assert result is None


def test_remove_member_deletes(session):
    svc = HousewifeFamilyService(session)
    m = svc.add_member(tenant_id="t1", user_id="u1", name="X", role="child")
    assert svc.remove_member(tenant_id="t1", user_id="u1", member_id=m.id) is True
    assert session.query(FamilyMember).count() == 0


def test_remove_member_unknown_returns_false(session):
    svc = HousewifeFamilyService(session)
    assert svc.remove_member(
        tenant_id="t1", user_id="u1", member_id="fm_bogus"
    ) is False


# ---------------------------------------------------------------------------
# Dedup by name (bugfix 2026-04-21 — LLM called add_family_members twice
# in the same session and produced 9 family rows for 5 distinct members)
# ---------------------------------------------------------------------------


def test_add_member_skips_when_name_already_exists(session):
    """Second add_member call with the same normalised name returns the
    existing row instead of inserting a duplicate."""
    svc = HousewifeFamilyService(session)
    first = svc.add_member(tenant_id="t1", user_id="u1", name="Катя", role="spouse")
    second = svc.add_member(tenant_id="t1", user_id="u1", name="катя", role="spouse")
    assert second.id == first.id
    assert svc.session.query(FamilyMember).filter_by(
        tenant_id="t1", user_id="u1"
    ).count() == 1


def test_add_member_case_and_whitespace_insensitive(session):
    """'Катя', 'КАТЯ' and '  катя  ' collapse to the same record."""
    svc = HousewifeFamilyService(session)
    svc.add_member(tenant_id="t1", user_id="u1", name="Катя", role="spouse")
    svc.add_member(tenant_id="t1", user_id="u1", name="КАТЯ", role="spouse")
    svc.add_member(tenant_id="t1", user_id="u1", name="  катя  ", role="spouse")
    assert svc.session.query(FamilyMember).filter_by(
        tenant_id="t1", user_id="u1"
    ).count() == 1


def test_add_member_same_name_different_user_not_duplicate(session):
    """Dedup is scoped per (tenant, user). Two distinct users can each
    have a "Катя"."""
    session.add(User(id="u2", tenant_id="t1", telegram_account_id="200"))
    session.commit()
    svc = HousewifeFamilyService(session)
    svc.add_member(tenant_id="t1", user_id="u1", name="Катя", role="spouse")
    svc.add_member(tenant_id="t1", user_id="u2", name="Катя", role="spouse")
    assert svc.session.query(FamilyMember).count() == 2


def test_add_members_batch_skips_existing(session):
    """The bug we observed on prod: LLM called add_family_members on
    two separate turns (onboarding + later request for meal plan),
    duplicating 4 members. Batch must check the DB state and skip
    entries that already exist."""
    svc = HousewifeFamilyService(session)
    # Seed the "onboarding" batch
    svc.add_members_batch(
        tenant_id="t1", user_id="u1",
        members=[
            {"name": "Катя", "role": "spouse"},
            {"name": "Николай", "role": "child"},
            {"name": "Никита", "role": "child"},
            {"name": "Лиза", "role": "child"},
        ],
    )
    # Second LLM call re-adds them all plus "self" — only self is new.
    result = svc.add_members_batch(
        tenant_id="t1", user_id="u1",
        members=[
            {"name": "Борис", "role": "self"},
            {"name": "Катя", "role": "spouse"},
            {"name": "Николай", "role": "child"},
            {"name": "Никита", "role": "child"},
            {"name": "Лиза", "role": "child"},
        ],
    )
    # Only "Борис" should actually persist as a new row.
    assert len(result) == 1
    assert result[0].name == "Борис"
    # Total household count is now 5, not 9.
    assert svc.session.query(FamilyMember).filter_by(
        tenant_id="t1", user_id="u1"
    ).count() == 5


def test_add_members_batch_dedups_within_input(session):
    """LLM passes "Катя" twice in the same batch — collapse to one."""
    svc = HousewifeFamilyService(session)
    result = svc.add_members_batch(
        tenant_id="t1", user_id="u1",
        members=[
            {"name": "Катя", "role": "spouse"},
            {"name": "катя", "role": "spouse"},
            {"name": "Николай", "role": "child"},
        ],
    )
    assert len(result) == 2
    names = {r.name for r in result}
    assert names == {"Катя", "Николай"}
