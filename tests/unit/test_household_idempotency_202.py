"""#202 семья 3 (household): оснащение add_member ключами идемпотентности (эталон recipes/checklists).

ctx-путь → FamilyMember.operation_id + normalized_title_hash; pre-check op_id (повтор tool_call +
морфовариант той же леммы в батче — add_members_batch коммитит каждого) + hash; легаси (ctx None) — None.
add_members_batch_detailed делегирует add_member → батч покрыт. Плайн-сессия (не conftest).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife import FamilyMember
from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from sreda.services.housewife_family import HousewifeFamilyService


@pytest.fixture()
def s():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(User(id="u1", tenant_id="t1"))
    sess.commit()
    yield sess
    sess.close()


def _ctx(step="s1"):
    return ToolRuntimeContext(
        operation_id="o1", execution_id="e1", step_id=step,
        tool_name="add_family_members", tenant_id="t1", user_id="u1", turn_key="tk1")


def test_add_member_ctx_populates_keys_202(s):
    svc = HousewifeFamilyService(s)
    with bind_tool_runtime(_ctx()):
        m = svc.add_member(tenant_id="t1", user_id="u1", name="Маша", role="child")
    s.refresh(m)
    assert m.operation_id is not None
    assert m.normalized_title_hash is not None


def test_add_member_ctx_replay_single_row_202(s):
    """Повтор add_member того же контента в ходе (тот же ctx) → 1 член + тот же id."""
    svc = HousewifeFamilyService(s)
    with bind_tool_runtime(_ctx()):
        a = svc.add_member(tenant_id="t1", user_id="u1", name="Петя", role="child")
        b = svc.add_member(tenant_id="t1", user_id="u1", name="Петя", role="child")
    assert a.id == b.id
    assert s.query(FamilyMember).count() == 1


def test_add_member_legacy_no_ctx_keys_none_202(s):
    svc = HousewifeFamilyService(s)
    m = svc.add_member(tenant_id="t1", user_id="u1", name="Дима", role="parent")
    s.refresh(m)
    assert m.operation_id is None


def test_household_policy_idempotent_202():
    from sreda.runtime.react_loop import _FAMILY_WRITE_POLICY
    assert _FAMILY_WRITE_POLICY["household"] == "idempotent"


def test_add_members_batch_morpho_no_crash_202(s):
    """Батч с морфовариантами одной леммы («Маша»+«Маши») НЕ роняет IntegrityError: _normalise_name
    (whitespace/регистр) их НЕ схлопывает, но op_id pre-check (add_member коммитит каждого) ловит
    flushed-дубль. op_id в БД уникальны."""
    svc = HousewifeFamilyService(s)
    with bind_tool_runtime(_ctx()):
        res = svc.add_members_batch_detailed(
            tenant_id="t1", user_id="u1",
            members=[{"name": "Маша", "role": "child"},
                     {"name": "Маши", "role": "child"}])
    assert res is not None  # не упало
    rows = s.query(FamilyMember).all()
    op_ids = [r.operation_id for r in rows if r.operation_id is not None]
    assert len(op_ids) == len(set(op_ids)), "op_id в БД уникальны (нет коллизии)"
    # by-name контракт #115 (Codex/субагент R1 MAJOR): морфо-дубль в created НЕ задваивается; если
    # лемматизация схлопнула — «Маши» помечен как duplicate_in_batch, иначе обе created (нет коллизии).
    from sreda.services.operation_id import compute_normalized_title_hash as _h
    if _h("Маша", entity_type="family_member", tenant_id="t1", user_id="u1") == \
       _h("Маши", entity_type="family_member", tenant_id="t1", user_id="u1"):
        assert len(rows) == 1, "одна лемма → одна строка"
        assert len(res.created) == 1, "created НЕ задваивает морфо-дубль (#115)"
        assert "Маши" in res.duplicates_in_batch, "морфо-дубль помечен duplicate_in_batch (#115)"
    created_ids = [c.id for c in res.created]
    assert len(created_ids) == len(set(created_ids)), "created не содержит одну строку дважды"


def test_add_member_hash_dedup_catches_morpho_202(s):
    """hash-pre-check ловит морфовариант мимо _normalise_name: «Маша» затем «Маши» РАЗНЫМИ ходами
    (разный step→разный op_id) → одна лемма → один hash → 2-й replay'ит 1-го (1 член)."""
    svc = HousewifeFamilyService(s)
    with bind_tool_runtime(_ctx(step="s1")):
        a = svc.add_member(tenant_id="t1", user_id="u1", name="Маша", role="child")
    with bind_tool_runtime(_ctx(step="s2")):
        b = svc.add_member(tenant_id="t1", user_id="u1", name="Маши", role="child")
    if a.normalized_title_hash != b.normalized_title_hash:
        pytest.skip("normalize_for_dedup не схлопнул морфоформы имени (pymorphy недоступна)")
    assert s.query(FamilyMember).count() == 1, "одна лемма → один hash → 2-й replay'ит 1-го"
    assert a.id == b.id
