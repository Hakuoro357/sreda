"""Unit tests for Sub-A12 Phase E #8b-2 — executor ledger/idempotency wiring.

Covers:
- LEDGER DISABLED: behaviour byte-for-byte as before; 0 ledger rows; tool
  asserts current_tool_runtime() is None.
- LEDGER ENABLED happy path: 2-step plan → 2 ledger rows 'committed';
  tool asserts current_tool_runtime().operation_id == expected.
- DURABLE-WRITE TIMEOUT → ledger row status 'unknown_pending'.
- CONCURRENT batch (2 parallel steps in one layer) → each step gets its
  own operation_id; they differ and each equals allocate_operation_id().
- Regression: test_planner_executor.py tests still pass (called separately
  via pytest CLI per spec).

Uses an in-memory SQLite engine (StaticPool) so all sessions share one
connection, mirroring test_step_ledger.py conventions exactly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Register ALL tables: FK chain Tenant→Workspace→AgentThread→AgentRun→
# PlannerExecution→StepExecutionLedger must all exist before create_all.
# sreda.db.models.__init__ imports most tables but misses checklists.py
# (which tasks.py's FK target 'checklists.id' lives in).  Without it,
# create_all raises NoReferencedTableError on the tasks_items FK.
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — registers core tables
import sreda.db.models.checklists  # noqa: F401 — registers checklists table (FK target for tasks)
import sreda.db.models.planner  # noqa: F401 — registers planner tables

from sreda.db.models import (
    AgentRun,
    AgentThread,
    PlannerExecution,
    Tenant,
    Workspace,
)
from sreda.db.models.planner import StepExecutionLedger
from sreda.runtime.planner.executor import ExecutorError, execute_plan
from sreda.runtime.planner.plan_compiler import compile as compile_plan
from sreda.runtime.planner.schemas import Plan
from sreda.runtime.planner.tool_runtime import (
    allocate_operation_id,
    current_tool_runtime,
)
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

# ---------------------------------------------------------------------------
# Fixed timestamp
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

REGISTRY = {s.name: s for s in MIGRATED_TOOL_SPECS}


# ---------------------------------------------------------------------------
# In-memory engine + session factory (module-scoped, StaticPool)
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    # Function-scoped (Codex A/B #8b-2 R1 MINOR): a per-test in-memory DB so
    # ledger rows never leak across tests — the disabled-path tests assert the
    # WHOLE ledger table is empty, which would be order-dependent under a
    # shared module engine. StaticPool keeps the single :memory: connection so
    # seed + ledger + assertion sessions all see the same data within a test.
    eng = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    """Return a sessionmaker bound to the (per-test) in-memory engine."""
    return sessionmaker(bind=engine)


@pytest.fixture
def db_session(engine):
    """Per-test read session for assertions (rolled back on teardown)."""
    conn = engine.connect()
    trans = conn.begin()
    sess = sessionmaker(bind=conn)()
    try:
        yield sess
    finally:
        sess.close()
        trans.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# Seed helper — exact copy of test_step_ledger._seed_execution
# ---------------------------------------------------------------------------


def _seed_execution(session: Session) -> str:
    """Insert minimal FK chain and return the PlannerExecution id."""
    tenant_id = f"tenant_{uuid4().hex[:8]}"
    workspace_id = f"ws_{uuid4().hex[:8]}"
    thread_id = f"thread_{uuid4().hex[:8]}"
    run_id = f"run_{uuid4().hex[:8]}"
    exec_id = f"pe_{uuid4().hex[:8]}"

    session.add(Tenant(id=tenant_id, name="t"))
    session.add(Workspace(id=workspace_id, tenant_id=tenant_id, name="w"))
    session.add(
        AgentThread(
            id=thread_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            channel_type="telegram",
            external_chat_id="42",
        )
    )
    session.add(
        AgentRun(
            id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            action_type="chat",
        )
    )
    session.add(
        PlannerExecution(
            id=exec_id,
            run_id=run_id,
            tenant_id=tenant_id,
            feature_key="housewife_assistant",
            planner_prompt_version=1,
            planner_provider="mimo-v2.5-pro",
            planner_model="mimo-v2.5-pro",
            planner_status="pending",
            execution_status="pending",
            execution_log_json=[],
            created_at=NOW,
        )
    )
    session.flush()
    session.commit()
    return exec_id


# ---------------------------------------------------------------------------
# Plan + action builder helpers (mirrors test_planner_executor.py style)
# ---------------------------------------------------------------------------


def _plan(actions: dict, *, compose: dict | None = None) -> Plan:
    if compose is None:
        compose = {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["x"]},
        }
    return Plan.model_validate(
        {
            "schema_version": 1,
            "turn_classification": {"is_new_turn": True, "reason": "test"},
            "clarity": "clear",
            "actions": actions,
            "compose": compose,
        }
    )


def _action(
    tool: str,
    *,
    args: dict | None = None,
    outcomes: list[dict] | None = None,
    intent_group: str = "default",
) -> dict:
    if args is None:
        args = {"items": [{"title": "x"}]} if tool == "add_shopping_items" else {}
    if outcomes is None:
        outcomes = [
            {
                "match": {"status": "added"},
                "next": None,
                "compose": {
                    "kind": "template",
                    "template_id": "shopping_added_ok",
                    "template_data": {"items": ["x"]},
                },
            },
        ]
    return {
        "tool": tool,
        "args": args,
        "expected_outcomes": outcomes,
        "intent_group": intent_group,
        "depends_on": [],
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Stub tool that captures current_tool_runtime() during invocation
# ---------------------------------------------------------------------------


class _CapturingTool:
    """Async stub that records the ToolRuntimeContext visible inside ainvoke.

    Sets ``self.seen_ctx`` on each call (last call wins for multi-step).
    Also accepts optional delay (seconds) for timeout tests.
    """

    def __init__(
        self,
        name: str,
        *,
        return_raw: str = "ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]",
        delay: float = 0.0,
    ):
        self.name = name
        self._return_raw = return_raw
        self._delay = delay
        self.seen_ctx: list[Any] = []  # one entry per invocation

    async def ainvoke(self, args: dict) -> str:
        self.seen_ctx.append(current_tool_runtime())
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._return_raw


# ---------------------------------------------------------------------------
# Test: LEDGER DISABLED — zero rows, ctx is None inside tool
# ---------------------------------------------------------------------------


class TestLedgerDisabled:
    def test_no_ledger_rows_created(self, engine) -> None:
        """Without execution_id/session_factory the ledger is never written."""
        plan = _plan({"s1": _action("add_shopping_items")})
        ep = compile_plan(plan, REGISTRY)
        tool = _CapturingTool("add_shopping_items")

        log = _run(
            execute_plan(
                ep,
                tools_by_name={"add_shopping_items": tool},
                registry=REGISTRY,
                # ledger params intentionally omitted → disabled
            )
        )

        assert log.steps[0].status == "ok"
        assert log.outcome == "completed"

        # No ledger rows written
        with Session(engine) as sess:
            rows = sess.execute(select(StepExecutionLedger)).scalars().all()
        assert rows == []

    def test_current_tool_runtime_is_none_inside_tool(self) -> None:
        """Tools running without ledger must see current_tool_runtime() = None."""
        plan = _plan({"s1": _action("add_shopping_items")})
        ep = compile_plan(plan, REGISTRY)
        tool = _CapturingTool("add_shopping_items")

        _run(
            execute_plan(
                ep,
                tools_by_name={"add_shopping_items": tool},
                registry=REGISTRY,
            )
        )

        assert len(tool.seen_ctx) == 1
        assert tool.seen_ctx[0] is None

    def test_two_step_plan_no_ledger(self, engine) -> None:
        """2-step plan without ledger runs fine; 0 rows."""
        plan = _plan(
            {
                "s1": _action("add_shopping_items"),
                "s2": _action(
                    "list_shopping",
                    args={},
                    outcomes=[
                        {
                            "match": {"status": "empty"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "shopping_list_empty",
                                "template_data": {},
                            },
                        }
                    ],
                ),
            }
        )
        ep = compile_plan(plan, REGISTRY)
        tool_add = _CapturingTool("add_shopping_items")
        tool_list = _CapturingTool("list_shopping", return_raw="no shopping items")

        log = _run(
            execute_plan(
                ep,
                tools_by_name={
                    "add_shopping_items": tool_add,
                    "list_shopping": tool_list,
                },
                registry=REGISTRY,
            )
        )
        assert log.outcome == "completed"

        with Session(engine) as sess:
            rows = sess.execute(select(StepExecutionLedger)).scalars().all()
        assert rows == []


# ---------------------------------------------------------------------------
# Test: LEDGER ENABLED happy path
# ---------------------------------------------------------------------------


class TestLedgerEnabled:
    def test_two_steps_both_committed(self, session_factory, engine) -> None:
        """2-step plan with ledger → 2 rows ending 'committed'."""
        # Seed FK chain using a plain session (committed so all ledger sessions
        # can see it — StaticPool shares one connection so this is visible).
        with Session(engine) as seed_sess:
            exec_id = _seed_execution(seed_sess)

        turn_key = f"turn_{uuid4().hex[:12]}"
        tenant_id = "tenant_test"

        plan = _plan(
            {
                "s1": _action("add_shopping_items"),
                "s2": _action(
                    "list_shopping",
                    args={},
                    outcomes=[
                        {
                            "match": {"status": "empty"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "shopping_list_empty",
                                "template_data": {},
                            },
                        }
                    ],
                ),
            }
        )
        ep = compile_plan(plan, REGISTRY)
        tool_add = _CapturingTool("add_shopping_items")
        tool_list = _CapturingTool("list_shopping", return_raw="no shopping items")

        log = _run(
            execute_plan(
                ep,
                tools_by_name={
                    "add_shopping_items": tool_add,
                    "list_shopping": tool_list,
                },
                registry=REGISTRY,
                execution_id=exec_id,
                turn_key=turn_key,
                tenant_id=tenant_id,
                ledger_session_factory=session_factory,
                now_fn=lambda: NOW,
            )
        )

        assert log.outcome == "completed"
        assert log.steps[0].status == "ok"
        assert log.steps[1].status == "ok"

        with Session(engine) as check:
            rows = (
                check.execute(
                    select(StepExecutionLedger).where(
                        StepExecutionLedger.execution_id == exec_id
                    )
                )
                .scalars()
                .all()
            )

        assert len(rows) == 2
        statuses = {r.step_id: r.status for r in rows}
        assert statuses["s1"] == "committed"
        assert statuses["s2"] == "committed"

    def test_operation_id_visible_inside_tool(self, session_factory, engine) -> None:
        """Tool sees current_tool_runtime().operation_id == allocate_operation_id()."""
        with Session(engine) as seed_sess:
            exec_id = _seed_execution(seed_sess)

        turn_key = f"turn_{uuid4().hex[:12]}"
        step_id = "s1"
        tool_name = "add_shopping_items"

        expected_op_id = allocate_operation_id(
            turn_key=turn_key,
            step_id=step_id,
            tool_name=tool_name,
        )

        plan = _plan({"s1": _action("add_shopping_items")})
        ep = compile_plan(plan, REGISTRY)
        tool = _CapturingTool("add_shopping_items")

        _run(
            execute_plan(
                ep,
                tools_by_name={"add_shopping_items": tool},
                registry=REGISTRY,
                execution_id=exec_id,
                turn_key=turn_key,
                tenant_id="tenant_test",
                ledger_session_factory=session_factory,
                now_fn=lambda: NOW,
            )
        )

        assert len(tool.seen_ctx) == 1
        ctx = tool.seen_ctx[0]
        assert ctx is not None
        assert ctx.operation_id == expected_op_id
        assert ctx.execution_id == exec_id
        assert ctx.step_id == step_id
        assert ctx.turn_key == turn_key


# ---------------------------------------------------------------------------
# Test: DURABLE-WRITE TIMEOUT → 'unknown_pending'
# ---------------------------------------------------------------------------


class TestDurableWriteTimeout:
    def test_durable_timeout_marks_unknown_pending(
        self, session_factory, engine
    ) -> None:
        """A durable-write tool that times out leaves the ledger row at
        'unknown_pending' (settle-window semantics — thread may still commit).
        """
        with Session(engine) as seed_sess:
            exec_id = _seed_execution(seed_sess)

        turn_key = f"turn_{uuid4().hex[:12]}"

        # add_shopping_items is effect='write', side_effect_class='transactional_write'
        # → is_durable_write=True.
        plan = _plan({"s1": _action("add_shopping_items")})
        ep = compile_plan(plan, REGISTRY)

        # Tool sleeps well past a tiny timeout
        tool = _CapturingTool("add_shopping_items", delay=5.0)

        # Shorten timeout so it fires quickly in the test
        spec = REGISTRY["add_shopping_items"].model_copy(update={"timeout_seconds": 1})
        fast_registry = dict(REGISTRY)
        fast_registry["add_shopping_items"] = spec

        log = _run(
            execute_plan(
                ep,
                tools_by_name={"add_shopping_items": tool},
                registry=fast_registry,
                execution_id=exec_id,
                turn_key=turn_key,
                tenant_id="tenant_test",
                ledger_session_factory=session_factory,
                now_fn=lambda: NOW,
                timeout_seconds_default=1.0,
            )
        )

        assert log.steps[0].status == "timeout"

        with Session(engine) as check:
            rows = (
                check.execute(
                    select(StepExecutionLedger).where(
                        StepExecutionLedger.execution_id == exec_id,
                        StepExecutionLedger.step_id == "s1",
                    )
                )
                .scalars()
                .all()
            )

        assert len(rows) == 1
        assert rows[0].status == "unknown_pending"


# ---------------------------------------------------------------------------
# Test: CONCURRENT batch — each step gets its own operation_id
# ---------------------------------------------------------------------------


class TestConcurrentBatch:
    def test_parallel_steps_get_distinct_operation_ids(
        self, session_factory, engine
    ) -> None:
        """Two parallel steps in one layer must bind distinct operation_ids.

        Each step's contextvar binding is local to that asyncio.gather coro —
        siblings do NOT share the var (verified fact #8b-2).
        """
        with Session(engine) as seed_sess:
            exec_id = _seed_execution(seed_sess)

        turn_key = f"turn_{uuid4().hex[:12]}"

        # Two independent add_shopping_items steps — no data deps between them,
        # so the compiler puts them in the same layer and gather runs them in
        # parallel.  Use different intent_groups so honest_partial batching
        # doesn't force them sequential.
        plan = _plan(
            {
                "s1": _action(
                    "add_shopping_items",
                    args={"items": [{"title": "apples"}]},
                    intent_group="group_a",
                ),
                "s2": _action(
                    "add_shopping_items",
                    args={"items": [{"title": "bread"}]},
                    intent_group="group_b",
                ),
            }
        )
        ep = compile_plan(plan, REGISTRY)

        # Both steps resolve to the same tools_by_name key (action.tool is
        # identical).  A single shared capturing stub records both invocations;
        # the order in seen_ctx matches asyncio.gather input order (= compiled
        # batch order, which mirrors execution_plan.layers order).
        shared_tool = _CapturingTool(
            "add_shopping_items",
            return_raw="ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]",
        )

        log = _run(
            execute_plan(
                ep,
                tools_by_name={"add_shopping_items": shared_tool},
                registry=REGISTRY,
                execution_id=exec_id,
                turn_key=turn_key,
                tenant_id="tenant_test",
                ledger_session_factory=session_factory,
                now_fn=lambda: NOW,
            )
        )

        assert log.outcome == "completed"
        assert len(log.steps) == 2
        assert all(s.status == "ok" for s in log.steps)

        # The two coros each run in their own copied context, so the
        # contextvar binding inside one does NOT leak to the sibling.
        # Each invocation must have seen a DIFFERENT operation_id.
        assert len(shared_tool.seen_ctx) == 2
        op_ids = [ctx.operation_id for ctx in shared_tool.seen_ctx if ctx is not None]
        assert len(op_ids) == 2
        assert op_ids[0] != op_ids[1], (
            "Parallel steps must get distinct operation_ids — "
            "contextvar leaked between gathered coros"
        )

        # Map captured contexts by ctx.step_id (NOT by call order — Codex A/B
        # #8b-2 R1 MINOR: gather scheduling order is not guaranteed to match
        # log.steps order). Each must equal allocate_operation_id for its step.
        seen_by_step = {
            ctx.step_id: ctx for ctx in shared_tool.seen_ctx if ctx is not None
        }
        assert set(seen_by_step) == {"s1", "s2"}
        for step_id, ctx in seen_by_step.items():
            expected = allocate_operation_id(
                turn_key=turn_key,
                step_id=step_id,
                tool_name="add_shopping_items",
            )
            assert ctx.operation_id == expected, (
                f"step {step_id}: got {ctx.operation_id!r}, "
                f"expected {expected!r}"
            )

        # Two distinct ledger rows
        with Session(engine) as check:
            rows = (
                check.execute(
                    select(StepExecutionLedger).where(
                        StepExecutionLedger.execution_id == exec_id
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 2
        row_op_ids = {r.operation_id for r in rows}
        assert len(row_op_ids) == 2  # distinct


# ---------------------------------------------------------------------------
# Test: precondition guard — empty turn_key raises ExecutorError
# ---------------------------------------------------------------------------


class TestPreconditionGuard:
    def test_empty_turn_key_raises_executor_error(self, session_factory, engine) -> None:
        """Ledger enabled but turn_key='' must raise ExecutorError at entry."""
        with Session(engine) as seed_sess:
            exec_id = _seed_execution(seed_sess)

        plan = _plan({"s1": _action("add_shopping_items")})
        ep = compile_plan(plan, REGISTRY)
        tool = _CapturingTool("add_shopping_items")

        with pytest.raises(ExecutorError, match="turn_key"):
            _run(
                execute_plan(
                    ep,
                    tools_by_name={"add_shopping_items": tool},
                    registry=REGISTRY,
                    execution_id=exec_id,
                    turn_key="",  # empty → must fail fast
                    tenant_id="tenant_test",
                    ledger_session_factory=session_factory,
                    now_fn=lambda: NOW,
                )
            )

    def test_none_turn_key_raises_executor_error(self, session_factory, engine) -> None:
        """Ledger enabled but turn_key=None must raise ExecutorError at entry."""
        with Session(engine) as seed_sess:
            exec_id = _seed_execution(seed_sess)

        plan = _plan({"s1": _action("add_shopping_items")})
        ep = compile_plan(plan, REGISTRY)
        tool = _CapturingTool("add_shopping_items")

        with pytest.raises(ExecutorError, match="turn_key"):
            _run(
                execute_plan(
                    ep,
                    tools_by_name={"add_shopping_items": tool},
                    registry=REGISTRY,
                    execution_id=exec_id,
                    turn_key=None,
                    tenant_id="tenant_test",
                    ledger_session_factory=session_factory,
                    now_fn=lambda: NOW,
                )
            )


# ---------------------------------------------------------------------------
# Stub that always raises inside ainvoke
# ---------------------------------------------------------------------------


class _RaisingTool:
    def __init__(self, name: str):
        self.name = name

    async def ainvoke(self, args: dict) -> str:
        raise RuntimeError("tool blew up")


# ---------------------------------------------------------------------------
# Test: durable failure transitions + non-durable leaves 'started' +
#        best-effort terminal-mark failure never crashes the plan
# (Codex A/B #8b-2 R1 MINOR — these are the risk-bearing transitions.)
# ---------------------------------------------------------------------------


def _ledger_rows(engine, exec_id: str, step_id: str) -> list:
    with Session(engine) as check:
        return (
            check.execute(
                select(StepExecutionLedger).where(
                    StepExecutionLedger.execution_id == exec_id,
                    StepExecutionLedger.step_id == step_id,
                )
            )
            .scalars()
            .all()
        )


class TestLedgerTerminalTransitions:
    def test_durable_error_marks_unknown(self, session_factory, engine) -> None:
        """A durable-write tool that RAISES → 'error' → ledger 'unknown'
        (it may have committed before raising; recovery must probe)."""
        with Session(engine) as s:
            exec_id = _seed_execution(s)
        plan = _plan({"s1": _action("add_shopping_items")})
        ep = compile_plan(plan, REGISTRY)
        log = _run(
            execute_plan(
                ep,
                tools_by_name={"add_shopping_items": _RaisingTool("add_shopping_items")},
                registry=REGISTRY,
                execution_id=exec_id,
                turn_key=f"turn_{uuid4().hex[:12]}",
                tenant_id="tenant_test",
                ledger_session_factory=session_factory,
                now_fn=lambda: NOW,
            )
        )
        assert log.steps[0].status == "error"
        rows = _ledger_rows(engine, exec_id, "s1")
        assert len(rows) == 1
        assert rows[0].status == "unknown"

    def test_durable_unknown_outcome_marks_unknown(
        self, session_factory, engine
    ) -> None:
        """A durable-write tool whose output matches NO branch → 'unknown_outcome'
        → ledger 'unknown'."""
        with Session(engine) as s:
            exec_id = _seed_execution(s)
        # Tool returns status 'added' but the action only matches 'empty' → no match.
        plan = _plan(
            {
                "s1": _action(
                    "add_shopping_items",
                    outcomes=[
                        {
                            "match": {"status": "empty"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "shopping_list_empty",
                                "template_data": {},
                            },
                        }
                    ],
                )
            }
        )
        ep = compile_plan(plan, REGISTRY)
        tool = _CapturingTool("add_shopping_items")  # returns status 'added'
        log = _run(
            execute_plan(
                ep,
                tools_by_name={"add_shopping_items": tool},
                registry=REGISTRY,
                execution_id=exec_id,
                turn_key=f"turn_{uuid4().hex[:12]}",
                tenant_id="tenant_test",
                ledger_session_factory=session_factory,
                now_fn=lambda: NOW,
            )
        )
        assert log.steps[0].status == "unknown_outcome"
        rows = _ledger_rows(engine, exec_id, "s1")
        assert rows[0].status == "unknown"

    def test_durable_plan_gap_marks_unknown(self, session_factory, engine) -> None:
        """A durable-write tool whose output violates the contract →
        'plan_gap' (process_output raised PlannerGapError) → ledger 'unknown'.
        The tool may have committed before the parser rejected its output, so
        recovery must probe. (Codex A/B #8b-2 R2 MINOR — plan_gap coverage.)"""
        with Session(engine) as s:
            exec_id = _seed_execution(s)
        plan = _plan({"s1": _action("add_shopping_items")})
        ep = compile_plan(plan, REGISTRY)
        # count=0 with non-empty ids → ToolOutputContractViolation →
        # PlannerGapError → status 'plan_gap' (see housewife.py contract).
        tool = _CapturingTool(
            "add_shopping_items",
            return_raw="ok:added:0:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]",
        )
        log = _run(
            execute_plan(
                ep,
                tools_by_name={"add_shopping_items": tool},
                registry=REGISTRY,
                execution_id=exec_id,
                turn_key=f"turn_{uuid4().hex[:12]}",
                tenant_id="tenant_test",
                ledger_session_factory=session_factory,
                now_fn=lambda: NOW,
            )
        )
        assert log.steps[0].status == "plan_gap"
        rows = _ledger_rows(engine, exec_id, "s1")
        assert len(rows) == 1
        assert rows[0].status == "unknown"

    def test_non_durable_error_leaves_started(self, session_factory, engine) -> None:
        """A NON-durable (read) tool that fails leaves the ledger row at
        'started' — no commit risk, so no terminal obligation."""
        with Session(engine) as s:
            exec_id = _seed_execution(s)
        plan = _plan(
            {
                "s1": _action(
                    "list_shopping",
                    args={},
                    outcomes=[
                        {
                            "match": {"status": "empty"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "shopping_list_empty",
                                "template_data": {},
                            },
                        }
                    ],
                )
            }
        )
        ep = compile_plan(plan, REGISTRY)
        log = _run(
            execute_plan(
                ep,
                tools_by_name={"list_shopping": _RaisingTool("list_shopping")},
                registry=REGISTRY,
                execution_id=exec_id,
                turn_key=f"turn_{uuid4().hex[:12]}",
                tenant_id="tenant_test",
                ledger_session_factory=session_factory,
                now_fn=lambda: NOW,
            )
        )
        assert log.steps[0].status == "error"
        rows = _ledger_rows(engine, exec_id, "s1")
        assert len(rows) == 1
        assert rows[0].status == "started"  # left as-is (read tool, no recovery)

    def test_terminal_mark_failure_does_not_crash_plan(
        self, session_factory, engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the terminal mark raises (DB blip), execute_plan must NOT raise,
        the StepResult must be unchanged ('ok'), and the 'started' row remains
        for the #9 scanner. (open_step is NOT patched, so 'started' persists.)"""
        with Session(engine) as s:
            exec_id = _seed_execution(s)

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("ledger terminal mark DB blip")

        # Patch the symbol the executor calls inside _best_effort_mark.
        monkeypatch.setattr(
            "sreda.runtime.planner.executor.mark_step_status", _boom
        )

        plan = _plan({"s1": _action("add_shopping_items")})
        ep = compile_plan(plan, REGISTRY)
        tool = _CapturingTool("add_shopping_items")

        # Must NOT raise despite the failing terminal mark.
        log = _run(
            execute_plan(
                ep,
                tools_by_name={"add_shopping_items": tool},
                registry=REGISTRY,
                execution_id=exec_id,
                turn_key=f"turn_{uuid4().hex[:12]}",
                tenant_id="tenant_test",
                ledger_session_factory=session_factory,
                now_fn=lambda: NOW,
            )
        )
        assert log.steps[0].status == "ok"  # StepResult unchanged
        assert log.outcome == "completed"
        rows = _ledger_rows(engine, exec_id, "s1")
        assert len(rows) == 1
        assert rows[0].status == "started"  # terminal mark failed → left at started
