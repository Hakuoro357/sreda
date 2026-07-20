"""R1-фиксы аудита 2026-07-18, область W2 (planner/checkpoint).

Покрывает находки decision-log R1:

- M5 step_ledger — mark_step_status разрешает recovery-повышение
     unknown/unknown_pending → committed (probe доказал запись); понижение
     committed по-прежнему запрещено.
- M7 validator — _reachable_producer_statuses при втором маршрутизаторе на
     того же consumer'а больше НЕ коротит на union-wide; возвращает прямые
     достижимые статусы продюсера (строго точнее).
- M8 react_checkpoint_saver — _prune_thread_locked сохраняет cp_id только что
     записанного put(), даже если он не в top-N по сортировке id.

(M6 grace — покрыт обновлёнными test_recovery_scanner; MINOR prompt_builder /
recovery_scanner reset — код-фиксы, регрессия смежными сьютами.)
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — регистрирует все таблицы
from sreda.db.models import AgentRun, AgentThread, Tenant, Workspace
from sreda.db.models.planner import PlannerExecution
from sreda.runtime.planner.step_ledger import mark_step_status, open_step
from sreda.runtime.planner.validator import _reachable_producer_statuses


# ---------------------------------------------------------------------------
# shared sqlite harness
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    trans = conn.begin()
    sess = sessionmaker(bind=conn)()
    try:
        yield sess
    finally:
        sess.close()
        trans.rollback()
        conn.close()


def _seed_execution(session: Session) -> str:
    from datetime import datetime, timezone

    tid = f"tenant_{uuid4().hex[:8]}"
    wid = f"ws_{uuid4().hex[:8]}"
    thid = f"thread_{uuid4().hex[:8]}"
    rid = f"run_{uuid4().hex[:8]}"
    eid = f"pe_{uuid4().hex[:8]}"
    session.add(Tenant(id=tid, name="t"))
    session.add(Workspace(id=wid, tenant_id=tid, name="w"))
    session.add(AgentThread(
        id=thid, tenant_id=tid, workspace_id=wid,
        channel_type="telegram", external_chat_id="42",
    ))
    session.add(AgentRun(
        id=rid, thread_id=thid, tenant_id=tid, workspace_id=wid, action_type="chat",
    ))
    session.add(PlannerExecution(
        id=eid, run_id=rid, tenant_id=tid, feature_key="housewife_assistant",
        planner_prompt_version=1, planner_provider="p", planner_model="m",
        planner_status="pending", execution_status="pending",
        execution_log_json=[], created_at=datetime.now(timezone.utc),
    ))
    session.flush()
    return eid


# ---------------------------------------------------------------------------
# M5 — recovery-повышение unknown → committed
# ---------------------------------------------------------------------------


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


@pytest.mark.parametrize("from_status", ["unknown", "unknown_pending"])
def test_m5_recovery_promotes_unknown_to_committed(session, from_status) -> None:
    """Recovery: probe доказал durable-запись → повышение unknown(_pending)
    → committed ДОЛЖНO примениться (раньше guard блокировал, строка навсегда
    оставалась unknown)."""
    eid = _seed_execution(session)
    open_step(session, execution_id=eid, step_id="s1", tool="t",
              operation_id="op1", now=_now())
    mark_step_status(session, execution_id=eid, step_id="s1",
                     status=from_status, now=_now())

    row = mark_step_status(session, execution_id=eid, step_id="s1",
                           status="committed", now=_now())
    assert row.status == "committed"  # повышение применилось


def test_m5_still_refuses_committed_demotion(session) -> None:
    """committed → unknown по-прежнему запрещено (защита от понижения)."""
    eid = _seed_execution(session)
    open_step(session, execution_id=eid, step_id="s2", tool="t",
              operation_id="op2", now=_now())
    mark_step_status(session, execution_id=eid, step_id="s2",
                     status="committed", now=_now())

    row = mark_step_status(session, execution_id=eid, step_id="s2",
                           status="unknown", now=_now())
    assert row.status == "committed"  # понижение отклонено, остаётся committed


# ---------------------------------------------------------------------------
# M7 — второй маршрутизатор больше не коротит на union-wide
# ---------------------------------------------------------------------------


def _branch(next_id: str, status: str | None):
    return SimpleNamespace(next=next_id, match=({"status": status} if status else {}))


def _plan(actions: dict):
    return SimpleNamespace(actions=actions)


def test_m7_second_router_returns_producer_direct_statuses() -> None:
    """На consumer C ведёт и P (status=ok), и второй маршрутизатор Q. Раньше
    функция коротила на set() → union-wide (ложная леность). Теперь возвращает
    прямые достижимые статусы P → {'ok'} (caller проверит поле в каждом)."""
    plan = _plan({
        "P": SimpleNamespace(expected_outcomes=[
            _branch("C", "ok"), _branch("X", "error")]),
        "Q": SimpleNamespace(expected_outcomes=[_branch("C", "done")]),
    })
    got = _reachable_producer_statuses(
        plan, producer_step_id="P", consumer_step_id="C")
    assert got == {"ok"}


def test_m7_single_router_unchanged() -> None:
    """Одиночный маршрутизатор: статусы прямых веток продюсера (без изменений)."""
    plan = _plan({
        "P": SimpleNamespace(expected_outcomes=[
            _branch("C", "ok"), _branch("C", "warn"), _branch("X", "error")]),
    })
    got = _reachable_producer_statuses(
        plan, producer_step_id="P", consumer_step_id="C")
    assert got == {"ok", "warn"}


def test_m7_no_route_to_consumer_is_empty() -> None:
    """Нет прямой ветки на consumer (root/data-dep) → пусто → caller union-wide."""
    plan = _plan({
        "P": SimpleNamespace(expected_outcomes=[_branch("X", "ok")]),
    })
    got = _reachable_producer_statuses(
        plan, producer_step_id="P", consumer_step_id="C")
    assert got == set()


# ---------------------------------------------------------------------------
# M8 — prune сохраняет cp_id текущего put()
# ---------------------------------------------------------------------------


def test_m8_prune_keeps_current_cp_id(session) -> None:
    """cp_id только что записанного put() с НИЗКИМ id (не в top-N по сортировке)
    не должен удаляться prune'ом в той же транзакции."""
    from sreda.runtime.react_checkpoint_saver import (
        PRUNE_KEEP_PER_THREAD,
        EncryptedSqlCheckpointSaver,
    )

    thread_id = "thr_m8"
    ns = ""
    # 26 «высоких» id (z01..z26) + 1 «низкий» текущий (a00) = 27 строк.
    high_ids = [f"z{n:02d}" for n in range(1, PRUNE_KEEP_PER_THREAD + 2)]
    current_low = "a00"
    all_ids = high_ids + [current_low]
    for cp in all_ids:
        session.execute(text(
            "INSERT INTO react_checkpoint "
            "(thread_id, checkpoint_ns, checkpoint_id, checkpoint_type, blob, "
            " metadata_type, metadata, created_at) "
            "VALUES (:t, :ns, :cp, 'x', :b, 'x', :b, '2026-07-18 10:00:00')"
        ), {"t": thread_id, "ns": ns, "cp": cp, "b": b"{}"})
    session.flush()

    EncryptedSqlCheckpointSaver._prune_thread_locked(
        session, thread_id, ns, keep_cp_id=current_low)

    remaining = {
        r[0] for r in session.execute(text(
            "SELECT checkpoint_id FROM react_checkpoint WHERE thread_id=:t"
        ), {"t": thread_id}).all()
    }
    # Текущий cp_id выжил, несмотря на низкий id.
    assert current_low in remaining
    # Обрезка всё же произошла: самый низкий из «высоких» (z01) вытеснен.
    assert "z01" not in remaining
    assert len(remaining) == PRUNE_KEEP_PER_THREAD + 1  # top-N + форс current
