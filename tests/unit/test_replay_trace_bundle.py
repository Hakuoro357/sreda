"""Unit tests for scripts/replay/trace_bundle.py (SIA Step 1, #152).

Covers ONLY the PURE assembly (``build_trace_bundle`` + helpers) with SYNTHETIC
fixtures — NO prod connection, NO real chat ids, NO DB, NO decryption keys.

Mirrors tests/unit/test_replay_probe_export.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPLAY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "replay"
if str(_REPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(_REPLAY_DIR))

from trace_bundle import (  # noqa: E402
    ExecutionStep,
    TraceBundle,
    build_trace_bundle,
)

_FAKE_TURN_ID = "run_synthetic_000000001"


def _ok_decrypt(value: str) -> str:
    """Stub: 'v2:fake:<plaintext-json>' → plaintext (no real key)."""
    if value.startswith("v2:fake:"):
        return value[len("v2:fake:"):]
    raise ValueError(f"unexpected fixture payload: {value!r}")


def _raw_diag(**overrides) -> dict:
    """A complete synthetic raw_diag dict for build_trace_bundle."""
    base = {
        # --- replay raw-turn side (consumed by build_export_record) ---
        "turn_id": _FAKE_TURN_ID,
        "input_json": {
            "source_type": "telegram_message",
            "source_value": "Покажи мои напоминания",
            "user_id": "user_1",
        },
        "created_at": "2026-05-10T14:23:00+03:00",
        "timezone_name": "Europe/Moscow",
        "locale": "ru",
        "profile": {"name": "Тест", "timezone": "Europe/Moscow", "language": "ru"},
        "memories": [],
        "history": {"active_turn": None, "closed_turns": []},
        "baseline_payloads": ['v2:fake:{"text": "Твои напоминания: ..."}'],
        "old_called_tools": [],
        "old_side_effects": [],
        "baseline_aligned": True,
        "trace_reliable": False,
        # --- diagnostic side (planner_executions row) ---
        "planner_execution": {
            "planner_status": "valid",
            "plan_json": {
                "actions": {
                    "s1": {"tool": "list_reminders", "args": {"title_match": None}},
                    "s2": {"tool": "schedule_reminder", "args": {"title": "разминка"}},
                }
            },
            "execution_log_json": [
                {"node_id": "s1", "status": "completed",
                 "outcome": {"status": "ok", "items": 3}},
                {"node_id": "s2", "status": "completed",
                 "outcome": {"status": "scheduled"}},
            ],
            "validation_errors": None,
            "composer_path": "template:reminders_list_show",
            "execution_status": "completed",
        },
    }
    base.update(overrides)
    return base


def test_builds_bundle_with_joined_steps() -> None:
    b = build_trace_bundle(_raw_diag(), decrypt=_ok_decrypt)
    # reused replay side carries the input
    assert b.replay.user_message == "Покажи мои напоминания"
    assert b.turn_id_hash == b.replay.turn_id_hash
    assert b.turn_id_hash and _FAKE_TURN_ID not in b.turn_id_hash  # opaque
    # diagnostic side
    assert b.planner_status == "valid"
    assert b.composer_path == "template:reminders_list_show"
    assert b.diag_status == {"plan": "ok", "execution": "ok"}
    # steps join tool+args (from plan) with status+outcome (from log)
    assert len(b.steps) == 2
    s1, s2 = b.steps
    assert (s1.node_id, s1.tool, s1.status) == ("s1", "list_reminders", "completed")
    assert s1.args == {"title_match": None}
    assert s1.outcome == {"status": "ok", "items": 3}
    assert (s2.node_id, s2.tool) == ("s2", "schedule_reminder")
    assert s2.args == {"title": "разминка"}


def test_roundtrips_through_dict() -> None:
    b = build_trace_bundle(_raw_diag(), decrypt=_ok_decrypt)
    restored = TraceBundle.from_dict(b.to_dict())
    assert restored.to_dict() == b.to_dict()
    assert restored.steps == b.steps
    assert restored.replay.user_message == b.replay.user_message


def test_missing_planner_execution_flags_not_crash() -> None:
    # A turn that failed before the planner stage: no planner_execution.
    b = build_trace_bundle(_raw_diag(planner_execution={}), decrypt=_ok_decrypt)
    assert b.diag_status == {"plan": "missing", "execution": "missing"}
    assert b.steps == ()
    assert b.plan_json is None
    # the replay (input) side is still assembled
    assert b.replay.user_message == "Покажи мои напоминания"


def test_invalid_plan_degrades_not_missing() -> None:
    # planner row exists but produced no valid plan (planner_status='invalid').
    pe = {
        "planner_status": "invalid",
        "plan_json": None,
        "execution_log_json": [],
        "validation_errors": "schema mismatch on s1.args",
        "composer_path": "",
        "execution_status": "pending",
    }
    b = build_trace_bundle(_raw_diag(planner_execution=pe), decrypt=_ok_decrypt)
    assert b.diag_status["plan"] == "degraded"
    assert b.planner_status == "invalid"
    assert b.validation_errors == "schema mismatch on s1.args"
    assert b.steps == ()


def test_plan_and_log_as_json_strings_coerced() -> None:
    # JSONB columns can arrive as strings depending on the read path.
    import json as _json

    pe = {
        "planner_status": "valid",
        "plan_json": _json.dumps(
            {"actions": {"s1": {"tool": "list_menu", "args": {}}}}
        ),
        "execution_log_json": _json.dumps(
            [{"node_id": "s1", "status": "completed", "outcome": {"status": "empty"}}]
        ),
        "validation_errors": None,
        "composer_path": "template:menu_empty",
        "execution_status": "completed",
    }
    b = build_trace_bundle(_raw_diag(planner_execution=pe), decrypt=_ok_decrypt)
    assert b.diag_status == {"plan": "ok", "execution": "ok"}
    assert len(b.steps) == 1
    assert b.steps[0].tool == "list_menu"
    assert b.steps[0].outcome == {"status": "empty"}


def test_step_id_and_parsed_output_fallback() -> None:
    # Realistic persisted shape: step_id (not node_id) + parsed_output (the
    # in-memory StepResult field), not "outcome". The join must still work.
    pe = {
        "planner_status": "valid",
        "plan_json": {"actions": {"s1": {"tool": "list_menu", "args": {"x": 1}}}},
        "execution_log_json": [
            {"step_id": "s1", "status": "completed", "parsed_output": {"status": "ok"}}
        ],
        "validation_errors": None, "composer_path": "", "execution_status": "completed",
    }
    b = build_trace_bundle(_raw_diag(planner_execution=pe), decrypt=_ok_decrypt)
    assert len(b.steps) == 1
    s = b.steps[0]
    assert (s.node_id, s.tool, s.args) == ("s1", "list_menu", {"x": 1})
    assert s.outcome == {"status": "ok"}  # from parsed_output fallback


def test_outcome_cascades_to_raw_output_when_parsed_none() -> None:
    # parsed_output present but None (failure path) → fall through to raw_output,
    # NOT return None (Codex R2 MAJOR: dict.get default doesn't skip a None value).
    pe = {
        "planner_status": "valid",
        "plan_json": {"actions": {"s1": {"tool": "x", "args": {}}}},
        "execution_log_json": [
            {"step_id": "s1", "status": "failed",
             "parsed_output": None, "raw_output": "error: boom"}
        ],
        "validation_errors": None, "composer_path": "", "execution_status": "failed",
    }
    b = build_trace_bundle(_raw_diag(planner_execution=pe), decrypt=_ok_decrypt)
    assert b.steps[0].outcome == "error: boom"


def test_non_dict_log_entries_filtered() -> None:
    pe = {
        "planner_status": "valid",
        "plan_json": {"actions": {"s1": {"tool": "list_menu", "args": {}}}},
        "execution_log_json": [
            {"node_id": "s1", "status": "completed", "outcome": {"ok": True}},
            "garbage",
            None,
        ],
        "validation_errors": None, "composer_path": "", "execution_status": "completed",
    }
    b = build_trace_bundle(_raw_diag(planner_execution=pe), decrypt=_ok_decrypt)
    assert len(b.steps) == 1  # only the dict entry survives


def test_planner_execution_as_json_string_coerced() -> None:
    import json as _json

    pe_str = _json.dumps({
        "planner_status": "valid",
        "plan_json": {"actions": {"s1": {"tool": "list_menu", "args": {}}}},
        "execution_log_json": [{"node_id": "s1", "status": "completed", "outcome": 1}],
        "validation_errors": None, "composer_path": "", "execution_status": "completed",
    })
    b = build_trace_bundle(_raw_diag(planner_execution=pe_str), decrypt=_ok_decrypt)
    assert b.diag_status == {"plan": "ok", "execution": "ok"}
    assert b.steps[0].tool == "list_menu"


def test_load_planner_execution_reads_encrypted(db_session) -> None:
    """Prod writes planner PII to the ``*_enc`` mirrors ONLY (encrypted_only);
    the loader must decrypt them via the ORM. A plaintext SELECT would return
    NULL (Codex + subagent R1 CRITICAL). Also pins tenant-scope."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from sreda.db.models import (
        AgentRun, AgentThread, PlannerExecution, Tenant, Workspace,
    )
    from trace_bundle import _load_planner_execution

    tid = f"tenant_{uuid4().hex[:8]}"
    wid, thid, rid = (f"ws_{uuid4().hex[:8]}", f"thread_{uuid4().hex[:8]}",
                      f"run_{uuid4().hex[:8]}")
    db_session.add(Tenant(id=tid, name="t"))
    db_session.add(Workspace(id=wid, tenant_id=tid, name="w"))
    db_session.add(AgentThread(id=thid, tenant_id=tid, workspace_id=wid,
                               channel_type="telegram", external_chat_id="42"))
    db_session.add(AgentRun(id=rid, thread_id=thid, tenant_id=tid,
                            workspace_id=wid, action_type="chat"))
    # Prod-shaped: plaintext PII NULL, *_enc populated (TypeDecorator encrypts).
    db_session.add(PlannerExecution(
        id=f"pe_{uuid4().hex[:8]}", run_id=rid, tenant_id=tid,
        feature_key="housewife_assistant", planner_prompt_version=1,
        planner_provider="mimo-v2.5-pro", planner_model="mimo-v2.5-pro",
        planner_status="valid", execution_status="completed",
        plan_json=None,
        plan_json_enc={"actions": {"s1": {"tool": "list_reminders", "args": {}}}},
        validation_errors=None,
        validation_errors_enc="schema mismatch on s1",
        execution_log_json=[],
        execution_log_json_enc=[
            {"node_id": "s1", "status": "completed", "outcome": {"items": 3}}
        ],
        composer_path="template:reminders_list_show",
        created_at=datetime.now(timezone.utc),
    ))
    db_session.flush()
    db_session.expire_all()  # force reload from DB → exercise the decrypt path

    loaded = _load_planner_execution(db_session, rid, tid)
    assert loaded["plan_json"] == {
        "actions": {"s1": {"tool": "list_reminders", "args": {}}}
    }
    assert loaded["execution_log_json"] == [
        {"node_id": "s1", "status": "completed", "outcome": {"items": 3}}
    ]
    assert loaded["composer_path"] == "template:reminders_list_show"
    assert loaded["validation_errors"] == "schema mismatch on s1"  # decrypted *_enc
    # wrong tenant → no row (defense-in-depth)
    assert _load_planner_execution(db_session, rid, "tenant_other") == {}


def _seed_run(db_session):
    from uuid import uuid4

    from sreda.db.models import AgentRun, AgentThread, Tenant, Workspace

    tid = f"tenant_{uuid4().hex[:8]}"
    wid, thid, rid = (f"ws_{uuid4().hex[:8]}", f"thread_{uuid4().hex[:8]}",
                      f"run_{uuid4().hex[:8]}")
    db_session.add(Tenant(id=tid, name="t"))
    db_session.add(Workspace(id=wid, tenant_id=tid, name="w"))
    db_session.add(AgentThread(id=thid, tenant_id=tid, workspace_id=wid,
                               channel_type="telegram", external_chat_id="42"))
    db_session.add(AgentRun(id=rid, thread_id=thid, tenant_id=tid,
                            workspace_id=wid, action_type="chat"))
    db_session.flush()
    return tid, rid


def test_persist_completed_turn_valid_roundtrips(db_session) -> None:
    """#155: persist_completed_turn writes ONE row that TraceBundle's loader
    reads back through encryption (writer ⋈ reader end-to-end)."""
    from uuid import uuid4

    from sreda.runtime.planner.persistence import persist_completed_turn
    from trace_bundle import _load_planner_execution

    tid, rid = _seed_run(db_session)
    persist_completed_turn(
        db_session,
        execution_id=f"pe_{uuid4().hex[:8]}", run_id=rid, tenant_id=tid,
        feature_key="housewife_assistant", planner_prompt_version=1,
        planner_provider="inception-mercury2", planner_model="mercury-2",
        planner_status="valid", execution_status="completed",
        plan_json={"actions": {"s1": {"tool": "list_reminders", "args": {}}}},
        execution_log=[{"step_id": "s1", "tool": "list_reminders", "status": "ok",
                        "parsed_output": {"items": 2}, "raw_output": None}],
        validation_errors=None,
        composer_path="template:reminders_list_show",
    )
    db_session.expire_all()  # force decrypt path

    loaded = _load_planner_execution(db_session, rid, tid)
    assert loaded["planner_status"] == "valid"
    assert loaded["plan_json"] == {"actions": {"s1": {"tool": "list_reminders", "args": {}}}}
    assert loaded["execution_log_json"][0]["tool"] == "list_reminders"
    assert loaded["execution_log_json"][0]["parsed_output"] == {"items": 2}
    assert loaded["composer_path"] == "template:reminders_list_show"


def test_persist_completed_turn_invalid_plan(db_session) -> None:
    """#155: the failure case (invalid plan) — the most valuable for SIA — is
    persisted: status=invalid, no plan, decrypted validation_errors."""
    from uuid import uuid4

    from sreda.runtime.planner.persistence import persist_completed_turn
    from trace_bundle import _load_planner_execution

    tid, rid = _seed_run(db_session)
    persist_completed_turn(
        db_session,
        execution_id=f"pe_{uuid4().hex[:8]}", run_id=rid, tenant_id=tid,
        feature_key="housewife_assistant", planner_prompt_version=1,
        planner_provider="inception-mercury2", planner_model="mercury-2",
        planner_status="invalid", execution_status="pending",
        plan_json=None, execution_log=[],
        validation_errors="schema mismatch on s1.args",
    )
    db_session.expire_all()

    loaded = _load_planner_execution(db_session, rid, tid)
    assert loaded["planner_status"] == "invalid"
    assert loaded["plan_json"] is None
    assert loaded["validation_errors"] == "schema mismatch on s1.args"
    assert loaded["execution_log_json"] == []


def test_persist_encrypted_only_physical_columns(db_session) -> None:
    """#155 privacy invariant (Codex R1): under encrypted_only the plaintext PII
    columns are NULL (execution_log_json stays []), real data lives in *_enc."""
    from uuid import uuid4

    from sreda.db.models import PlannerExecution
    from sreda.runtime.planner.persistence import persist_completed_turn

    tid, rid = _seed_run(db_session)
    eid = f"pe_{uuid4().hex[:8]}"
    persist_completed_turn(
        db_session, execution_id=eid, run_id=rid, tenant_id=tid,
        feature_key="hw", planner_prompt_version=1, planner_provider="x",
        planner_model="", planner_status="valid", execution_status="completed",
        plan_json={"actions": {"s1": {"tool": "t", "args": {}}}},
        execution_log=[{"step_id": "s1", "tool": "t", "status": "ok"}],
        validation_errors="oops",
    )
    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)
    # plaintext PII columns NULL / empty
    assert row.plan_json is None
    assert row.validation_errors is None
    assert row.execution_log_json == []  # NOT NULL col → empty, not NULL
    # *_enc mirrors decrypt to the real data
    assert row.plan_json_enc == {"actions": {"s1": {"tool": "t", "args": {}}}}
    assert row.validation_errors_enc == "oops"
    assert row.execution_log_json_enc == [{"step_id": "s1", "tool": "t", "status": "ok"}]


def test_step_without_plan_action_keeps_outcome() -> None:
    # An execution-log step whose node_id is absent from plan.actions still
    # surfaces its status/outcome (tool/args empty) — never dropped.
    pe = {
        "planner_status": "valid",
        "plan_json": {"actions": {}},
        "execution_log_json": [
            {"node_id": "s9", "status": "failed", "outcome": {"error": "boom"}}
        ],
        "validation_errors": None,
        "composer_path": "",
        "execution_status": "failed",
    }
    b = build_trace_bundle(_raw_diag(planner_execution=pe), decrypt=_ok_decrypt)
    assert len(b.steps) == 1
    assert b.steps[0] == ExecutionStep(
        node_id="s9", tool="", args={}, status="failed",
        outcome={"error": "boom"},
    )
