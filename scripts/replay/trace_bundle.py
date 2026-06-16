"""TraceBundle v1 — full diagnostic trace of ONE conversation turn.

SIA feedback loop, Step 1 (vex-assistant#152). "Собрать ошибку в одно место":
given a turn, see everything — user → plan → tools(args+outcome) → reply.

The offline-replay export (:class:`reconstruct.ReplayExportRecord`) already
captures the INPUT side (user message, profile/memory/history context) plus a
thin baseline (old reply + tool NAMES). TraceBundle v1 REUSES that and adds the
DIAGNOSTIC side from ``planner_executions`` (already in the DB):
  * the validated plan (``plan_json``),
  * per-step execution joined with the plan: tool + args + status + outcome,
  * planner/execution status, validation errors, composer path.

Structure mirrors ``probe_replay_export`` for testability + safety:
  * :func:`build_trace_bundle` — PURE assembly (raw row dict → TraceBundle).
    No DB, no network — unit-tested with synthetic fixtures.
  * :func:`fetch_diag_turns` — thin read-only DB layer that adds the
    ``planner_executions`` row to each replay raw-turn (runs on the VDS).

PII discipline is inherited from ``probe_replay_export``: ids are SHA-256
hashed, the tenant is a redacted placeholder, and the assembled bundle is PII
until redacted — it must live in the quarantine dir and NEVER be committed.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Import the replay export contract + helpers (pure — no side effects on import).
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from probe_replay_export import (  # noqa: E402
    TENANT_PLACEHOLDER,
    _as_dict,
    build_export_record,
    fetch_raw_turns,
)
from reconstruct import ReplayExportRecord  # noqa: E402

# Per-field completeness vocabulary (must match reconstruct.py / export).
_OK = "ok"
_MISSING = "missing"
_DEGRADED = "degraded"


# ---------------------------------------------------------------------------
# Diagnostic sub-records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionStep:
    """One executed plan step: the tool + its args (from ``plan_json``) joined
    with the step's runtime status + outcome (from ``execution_log_json``)."""

    node_id: str
    tool: str
    args: dict[str, Any]
    status: str          # step status, e.g. "completed" | "failed" | "skipped"
    outcome: Any         # raw step outcome (dict / str / None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecutionStep":
        return cls(
            node_id=str(d.get("node_id") or ""),
            tool=str(d.get("tool") or ""),
            args=dict(d.get("args") or {}),
            status=str(d.get("status") or ""),
            outcome=d.get("outcome"),
        )


@dataclass(frozen=True)
class TraceBundle:
    """v1 diagnostic trace of one turn = replay context (input + baseline) +
    the planner diagnostic (plan, per-step execution, statuses)."""

    turn_id_hash: str
    tenant_placeholder: str

    # Input + context + baseline reply (reused — carries its own
    # reconstruction_status for the input side).
    replay: ReplayExportRecord

    # --- planner diagnostic (from planner_executions) ---
    planner_status: str        # pending | received | invalid | valid
    plan_json: dict[str, Any] | None
    validation_errors: str
    composer_path: str
    execution_status: str
    steps: tuple[ExecutionStep, ...]

    # Completeness of the DIAGNOSTIC side (the input side lives in
    # ``replay.reconstruction_status``). Keys: "plan", "execution".
    diag_status: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id_hash": self.turn_id_hash,
            "tenant_placeholder": self.tenant_placeholder,
            "replay": self.replay.to_dict(),
            "planner_status": self.planner_status,
            "plan_json": self.plan_json,
            "validation_errors": self.validation_errors,
            "composer_path": self.composer_path,
            "execution_status": self.execution_status,
            "steps": [s.to_dict() for s in self.steps],
            "diag_status": self.diag_status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TraceBundle":
        required = (
            "turn_id_hash", "tenant_placeholder", "replay", "planner_status",
            "plan_json", "validation_errors", "composer_path",
            "execution_status", "steps", "diag_status",
        )
        missing = [k for k in required if k not in d]
        if missing:
            raise KeyError(f"TraceBundle missing required field(s): {missing}")
        return cls(
            turn_id_hash=d["turn_id_hash"],
            tenant_placeholder=d["tenant_placeholder"],
            replay=ReplayExportRecord.from_dict(d["replay"]),
            planner_status=d["planner_status"],
            plan_json=d["plan_json"],
            validation_errors=d["validation_errors"],
            composer_path=d["composer_path"],
            execution_status=d["execution_status"],
            steps=tuple(ExecutionStep.from_dict(s) for s in d["steps"]),
            diag_status=d["diag_status"],
        )


# ---------------------------------------------------------------------------
# PURE assembly: raw row dict → TraceBundle
# ---------------------------------------------------------------------------


def _coerce_plan(plan_raw: Any) -> dict[str, Any] | None:
    """``plan_json`` may arrive as a dict (JSONB) or a JSON string. Returns a
    dict, or None when absent / unparseable / not an object."""
    if isinstance(plan_raw, dict):
        return plan_raw
    if isinstance(plan_raw, str) and plan_raw.strip():
        parsed = _as_dict(plan_raw)
        return parsed or None
    return None


def _coerce_execution_log(log_raw: Any) -> list[dict[str, Any]]:
    """``execution_log_json`` may be a list (JSONB) or a JSON string."""
    if isinstance(log_raw, list):
        return [e for e in log_raw if isinstance(e, dict)]
    if isinstance(log_raw, str) and log_raw.strip():
        try:
            parsed = json.loads(log_raw)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return [e for e in parsed if isinstance(e, dict)]
    return []


def _extract_steps(
    plan_json: dict[str, Any] | None,
    execution_log: list[dict[str, Any]],
) -> tuple[ExecutionStep, ...]:
    """Join plan actions (tool + args) with execution-log step outcomes.

    ``plan_json.actions`` maps ``node_id → {tool, args}``; each execution-log
    entry carries ``{node_id, status, outcome}``. We key by node_id so a step's
    tool/args (intent) sit next to what actually happened (outcome)."""
    actions = (plan_json or {}).get("actions") or {}
    steps: list[ExecutionStep] = []
    for entry in execution_log:
        node_id = str(entry.get("node_id") or entry.get("step_id") or "")
        action = actions.get(node_id) if isinstance(actions, dict) else None
        action = action if isinstance(action, dict) else {}
        steps.append(
            ExecutionStep(
                node_id=node_id,
                tool=str(action.get("tool") or ""),
                args=dict(action.get("args") or {}),
                status=str(entry.get("status") or ""),
                outcome=entry.get("outcome"),
            )
        )
    return tuple(steps)


def build_trace_bundle(
    raw_diag: dict[str, Any],
    *,
    decrypt: Callable[[str], str],
    tenant_placeholder: str = TENANT_PLACEHOLDER,
) -> TraceBundle:
    """Assemble a :class:`TraceBundle` from an already-fetched raw row.

    PURE — NO DB, NO network. This is the unit-tested core.

    ``raw_diag`` is a ``probe_replay_export.fetch_raw_turns`` row dict PLUS a
    ``"planner_execution"`` key holding the ``planner_executions`` row as a dict
    (``{planner_status, plan_json, execution_log_json, validation_errors,
    composer_path, execution_status}``). A missing/empty ``planner_execution``
    yields ``diag_status`` flags rather than a crash (a turn can fail before the
    planner stage).
    """
    replay = build_export_record(
        raw_diag, decrypt=decrypt, tenant_placeholder=tenant_placeholder
    )

    pe = _as_dict(raw_diag.get("planner_execution"))
    plan_json = _coerce_plan(pe.get("plan_json"))
    execution_log = _coerce_execution_log(pe.get("execution_log_json"))
    steps = _extract_steps(plan_json, execution_log)

    diag_status: dict[str, str] = {}
    if not pe:
        diag_status["plan"] = _MISSING
    elif plan_json:
        diag_status["plan"] = _OK
    else:
        # planner row exists but no parsed plan (e.g. planner_status='invalid')
        diag_status["plan"] = _DEGRADED
    diag_status["execution"] = _OK if steps else _MISSING

    return TraceBundle(
        turn_id_hash=replay.turn_id_hash,
        tenant_placeholder=tenant_placeholder,
        replay=replay,
        planner_status=str(pe.get("planner_status") or ""),
        plan_json=plan_json,
        validation_errors=str(pe.get("validation_errors") or ""),
        composer_path=str(pe.get("composer_path") or ""),
        execution_status=str(pe.get("execution_status") or ""),
        steps=steps,
        diag_status=diag_status,
    )


# ---------------------------------------------------------------------------
# Thin read-only DB layer (runs on the VDS; not unit-tested with a real DB)
# ---------------------------------------------------------------------------


def _load_planner_execution(session: Any, run_id: str) -> dict[str, Any]:
    """Latest ``planner_executions`` row for ``run_id`` → dict (or ``{}``).

    Read-only. Picks the most recent row (retries create multiple rows per run).
    """
    from sqlalchemy import text

    row = session.execute(
        text(
            "SELECT planner_status, plan_json, execution_log_json, "
            "validation_errors, composer_path, execution_status "
            "FROM planner_executions WHERE run_id = :r "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"r": run_id},
    ).fetchone()
    if row is None:
        return {}
    planner_status, plan_json, execution_log_json, validation_errors, \
        composer_path, execution_status = row
    return {
        "planner_status": planner_status,
        "plan_json": plan_json,
        "execution_log_json": execution_log_json,
        "validation_errors": validation_errors,
        "composer_path": composer_path,
        "execution_status": execution_status,
    }


def fetch_diag_turns(
    session: Any,
    *,
    tenant_id: str,
    decrypt: Callable[[str], str],
    **kwargs: Any,
):
    """Read-only: yield ``raw_diag`` dicts (replay raw-turn + planner_execution).

    Wraps ``probe_replay_export.fetch_raw_turns`` and attaches the
    ``planner_executions`` row per run. Extra kwargs (``limit``, ``turn_ids``,
    ``since``, ``until``) pass through.
    """
    for raw_turn in fetch_raw_turns(
        session, tenant_id=tenant_id, decrypt=decrypt, **kwargs
    ):
        run_id = str(raw_turn.get("turn_id") or "")
        raw_turn["planner_execution"] = (
            _load_planner_execution(session, run_id) if run_id else {}
        )
        yield raw_turn
