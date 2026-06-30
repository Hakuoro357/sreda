"""#202 семья 4 (menu): state-идемпотентность БЕЗ op_id (plan_week — UPSERT по неделе).

menu не требует op_id/hash/миграции: plan_week удаляет прежний план недели + создаёт заново (merge)
→ повтор НЕ дублирует; set_cell по (plan,day,meal); clear_menu по неделе. Это «idempotent» по состоянию
(как shopping mark/clear). Доказываем: перевыпуск plan_week/clear не плодит строки. Плайн-сессия.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife_food import MenuPlan
from sreda.services.housewife_menu import HousewifeMenuService


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


def _cells():
    return [{"day_of_week": 0, "meal_type": "dinner", "free_text": "борщ"}]


def test_menu_policy_idempotent_202():
    from sreda.runtime.react_loop import _FAMILY_WRITE_POLICY
    assert _FAMILY_WRITE_POLICY["menu"] == "idempotent"


def test_plan_week_reissue_single_plan_202(s):
    """Перевыпуск plan_week той же недели → РОВНО 1 MenuPlan (upsert удаляет прежний)."""
    svc = HousewifeMenuService(s)
    svc.plan_week(tenant_id="t1", user_id="u1", week_start="2030-06-17", cells=_cells())
    svc.plan_week(tenant_id="t1", user_id="u1", week_start="2030-06-17", cells=_cells())
    plans = s.query(MenuPlan).filter_by(tenant_id="t1", user_id="u1").all()
    assert len(plans) == 1, f"перевыпуск недели не должен плодить планы: {len(plans)}"


def test_plan_week_distinct_weeks_two_plans_202(s):
    """Разные недели → 2 плана (не схлопываются)."""
    svc = HousewifeMenuService(s)
    svc.plan_week(tenant_id="t1", user_id="u1", week_start="2030-06-17", cells=_cells())
    svc.plan_week(tenant_id="t1", user_id="u1", week_start="2030-06-24", cells=_cells())
    assert s.query(MenuPlan).filter_by(tenant_id="t1", user_id="u1").count() == 2


def test_clear_menu_reissue_noop_202(s):
    """Повтор clear_menu → второй раз 0 удалено (идемпотентно)."""
    svc = HousewifeMenuService(s)
    svc.plan_week(tenant_id="t1", user_id="u1", week_start="2030-06-17", cells=_cells())
    assert svc.clear_menu(tenant_id="t1", user_id="u1", week_start="2030-06-17") == 1
    assert svc.clear_menu(tenant_id="t1", user_id="u1", week_start="2030-06-17") == 0
    assert s.query(MenuPlan).filter_by(tenant_id="t1", user_id="u1").count() == 0
