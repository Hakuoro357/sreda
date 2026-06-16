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

import pytest  # noqa: E402

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
