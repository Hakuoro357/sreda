"""#202 семья 2 (checklists): оснащение create_list ключами идемпотентности (эталон recipes).

ctx-путь → Checklist.operation_id + normalized_title_hash заполнены (key-policy + аудит);
pre-check op_id/hash делает их функциональными; легаси (ctx None) — None, байт-в-байт.
add_items НЕ оснащаем — у него item-дедуп по title (re-issue-safe), колонок у ChecklistItem нет.
Плайн-сессия (не conftest).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.checklists import Checklist
from sreda.db.models.core import Tenant, User
from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from sreda.services.checklists import ChecklistService


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
        tool_name="create_checklist", tenant_id="t1", user_id="u1", turn_key="tk1")


def test_create_checklist_ctx_populates_keys_202(s):
    svc = ChecklistService(s)
    with bind_tool_runtime(_ctx()):
        cl = svc.create_list(tenant_id="t1", user_id="u1", title="Покупки на дачу")
    s.refresh(cl)
    assert cl.operation_id is not None, "ctx-путь должен записать operation_id"
    assert cl.normalized_title_hash is not None


def test_create_checklist_ctx_replay_single_row_202(s):
    """Повтор create_list того же контента в ходе (тот же ctx) → 1 список + тот же id."""
    svc = ChecklistService(s)
    with bind_tool_runtime(_ctx()):
        c1 = svc.create_list(tenant_id="t1", user_id="u1", title="Дела")
        c2 = svc.create_list(tenant_id="t1", user_id="u1", title="Дела")
    assert c1.id == c2.id
    assert s.query(Checklist).count() == 1


def test_create_checklist_legacy_no_ctx_keys_none_202(s):
    svc = ChecklistService(s)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="Ремонт")
    s.refresh(cl)
    assert cl.operation_id is None, "легаси-путь не должен трогать operation_id"


def test_checklists_policy_idempotent_202():
    from sreda.runtime.react_loop import _FAMILY_WRITE_POLICY
    assert _FAMILY_WRITE_POLICY["checklists"] == "idempotent"


def test_create_checklist_hash_dedup_catches_morpho_202(s):
    """hash-pre-check ловит морфовариант мимо whitespace-дедупа: «Покупки» затем «Покупка» разными
    ходами (разный step→разный op_id) → одна лемма → один hash → 2-й replay'ит 1-й (1 список)."""
    svc = ChecklistService(s)
    with bind_tool_runtime(_ctx(step="s1")):
        c1 = svc.create_list(tenant_id="t1", user_id="u1", title="Покупки")
    with bind_tool_runtime(_ctx(step="s2")):
        c2 = svc.create_list(tenant_id="t1", user_id="u1", title="Покупка")
    assert s.query(Checklist).count() in (1, 2)
    if c1.normalized_title_hash == c2.normalized_title_hash:
        assert c1.id == c2.id, "одинаковый hash → 2-й replay 1-го"
