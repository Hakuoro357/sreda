"""End-to-end snapshot tests for the planner — Sub-A12 Phase B.6.

Runs every curated few-shot example (from ``few_shot_examples.py``) through
the full orchestrator pipeline with a mocked LLM that returns the
fixture's expected plan JSON. Verifies:

* `Plan.model_validate` passes
* `validate_plan(plan, registry)` returns no violations
* `compile(plan, registry)` produces an ExecutionPlan
* orchestrator persists the row with `planner_status='valid'`

This is the contract for "if the planner LLM returns plan X, the
orchestrator accepts it end-to-end". Failure here surfaces breaks
between the curated few-shot data and any of: schema (Sub-A1),
validator (Phase B.1 + B.3 phase-3 branch-status check), or compiler
(Phase B.3) — drift in any of those would fail snapshot.

Phase B.6 ships with the 7 curated few-shot examples as the snapshot
corpus. Real-Boris fixtures live under gitignored
``private/planner_fixtures/boris_real/`` and load only when
``SREDA_PLANNER_BORIS_FIXTURES_ENABLED=true`` (see plan).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from sreda.runtime.planner.few_shot_examples import all_examples
from sreda.runtime.planner.llm import PlannerCallResult
from sreda.runtime.planner.orchestrator import PlannerContext, run
from sreda.runtime.planner.prompt_builder import NowMoment, ProfileSnapshot
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------


def _private_fixture_dir() -> Path | None:
    """Return path to gitignored real-data fixtures, or None when not
    enabled / not present."""
    if os.environ.get("SREDA_PLANNER_BORIS_FIXTURES_ENABLED", "").lower() != "true":
        return None
    candidate = Path(__file__).resolve().parents[3] / "private" / "planner_fixtures" / "boris_real"
    return candidate if candidate.is_dir() else None


class PrivateFixtureError(RuntimeError):
    """Raised when a private real-Boris fixture is malformed and the
    env flag is on. Codex B.6 R1 MEDIUM: silent skip would hide partial
    corpus disappearance."""


def _load_private_fixtures() -> list[tuple[str, dict]]:
    """Load real-Boris fixtures iff the gitignored dir exists. Each
    JSON file has shape ``{user_message, context_brief, expected_plan}``.

    Codex B.6 R1 MEDIUM fix: when env flag is on AND dir exists, any
    malformed/missing-key file fails LOUDLY (raises PrivateFixtureError).
    Silent skipping would let a partial corpus look green."""
    out: list[tuple[str, dict]] = []
    pdir = _private_fixture_dir()
    if pdir is None:
        return out
    for path in sorted(pdir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PrivateFixtureError(
                f"malformed private fixture {path.name!r}: {exc}"
            ) from exc
        if not all(k in payload for k in ("user_message", "expected_plan")):
            raise PrivateFixtureError(
                f"private fixture {path.name!r} missing required keys "
                f"(user_message, expected_plan); got keys: "
                f"{sorted(payload.keys())}"
            )
        out.append((f"boris_real:{path.stem}", payload))
    return out


def _synthetic_fixtures() -> list[tuple[str, dict]]:
    """Curated synthetic fixtures from few_shot_examples.py — same data
    is in the planner system prompt. Reused here as the snapshot corpus
    so if a schema/registry change breaks one, we catch it both in
    prompt rendering AND end-to-end."""
    return [
        (
            f"synthetic:{i:02d}:{ex.user_message[:30]}",
            {
                "user_message": ex.user_message,
                "context_brief": ex.context_brief,
                "expected_plan": ex.plan,
            },
        )
        for i, ex in enumerate(all_examples(), start=1)
    ]


def _all_fixtures() -> list[tuple[str, dict]]:
    return _synthetic_fixtures() + _load_private_fixtures()


# ---------------------------------------------------------------------------
# Orchestrator-driver helpers
# ---------------------------------------------------------------------------


def _make_ctx(user_message: str) -> PlannerContext:
    return PlannerContext(
        tenant_id="tenant_snapshot_test",
        run_id="run_snapshot",
        feature_key="housewife_assistant",
        user_message=user_message,
        voice_meta=None,
        now=NowMoment(datetime(2026, 5, 28, 14, 30)),
        profile=ProfileSnapshot(name="Test"),
        memories=(),
        active_turn=None,
        closed_turns=(),
        available_tools=tuple(MIGRATED_TOOL_SPECS),
        composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
        # LLM compose path is behind a kill-switch (default off) and no
        # few-shot uses kind='llm' yet → empty allowlist here, matching
        # the default template-only production posture.
        composer_llm_prompt_keys=(),
        composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
        tool_registry_version="snap-v1",
        few_shot_block="",
        planner_provider="mimo-v2.5",
    )


def _make_session_factory(db_session: Session):
    class _SessionProxy:
        def __init__(self, session: Session) -> None:
            self._session = session

        def __enter__(self) -> Session:
            return self._session

        def __exit__(self, *exc_info: Any) -> None:
            pass

    def factory():
        return _SessionProxy(db_session)

    return factory


def _row(db_session: Session, eid: str) -> dict:
    res = db_session.execute(
        text("SELECT * FROM planner_executions WHERE id = :id"),
        {"id": eid},
    ).mappings().first()
    return dict(res) if res else {}


# ---------------------------------------------------------------------------
# Snapshot tests — parametrized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,fixture",
    _all_fixtures(),
    ids=[label for label, _ in _all_fixtures()],
)
def test_planner_snapshot_runs_through_orchestrator(
    label: str, fixture: dict, db_session: Session,
) -> None:
    """For each curated snapshot, mock LLM to return the expected plan
    and assert the orchestrator pipeline accepts it (parse + validate +
    compile + persist)."""
    user_message = fixture["user_message"]
    expected_plan = fixture["expected_plan"]

    ctx = _make_ctx(user_message)
    session_factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return PlannerCallResult(
            raw_text=json.dumps(expected_plan, ensure_ascii=False),
            latency_ms=42,
            provider="mimo-v2.5",
            model="mimo-v2.5-pro",
            attempt_no=1,
        )

    result = asyncio.run(run(
        ctx, session_factory=session_factory, call_planner_fn=fake_call,
        invalid_retry_enabled=False,  # one-shot — fixture data is canonical
    ))
    db_session.commit()

    assert result.success, (
        f"snapshot {label!r} failed: error={result.error_summary!r}"
    )
    assert result.plan is not None
    assert result.execution_plan is not None

    # Codex B.6 R1 MEDIUM fix: persistence checks must STRICTLY compare
    # stored JSON to the planner-result snapshot, not just "non-null".
    # If mark_valid wrote wrong/truncated JSON, this surfaces it.
    row = _row(db_session, result.execution_id)
    assert row["planner_status"] == "valid"

    stored_plan = row["plan_json"]
    if isinstance(stored_plan, str):
        stored_plan = json.loads(stored_plan)  # SQLite stores JSON as TEXT
    expected_plan_dump = result.plan.model_dump(mode="json")
    assert stored_plan == expected_plan_dump, (
        f"snapshot {label!r}: stored plan_json drifted from result.plan"
    )

    stored_exec_plan = row["execution_plan_json"]
    if isinstance(stored_exec_plan, str):
        stored_exec_plan = json.loads(stored_exec_plan)
    assert stored_exec_plan == {
        "layers": [list(layer) for layer in result.execution_plan.layers],
        "fail_modes": dict(result.execution_plan.fail_modes),
    }, (
        f"snapshot {label!r}: stored execution_plan_json drifted from "
        f"compiled ExecutionPlan"
    )

    # Schema parity: clarity round-trips through orchestrator
    assert result.plan.clarity == expected_plan["clarity"]
    if expected_plan["clarity"] == "needs_clarification":
        assert result.plan.clarity_reason == expected_plan["clarity_reason"]


def test_snapshot_corpus_includes_synthetic_baseline() -> None:
    """Sanity guard: at least 7 synthetic fixtures present. If the few-shot
    list shrinks below baseline, this snapshot test loses coverage."""
    synth = _synthetic_fixtures()
    assert len(synth) >= 7, (
        f"synthetic snapshot corpus too small ({len(synth)}); B.6 expects ≥7"
    )


def test_snapshot_private_dir_skipped_when_env_unset(monkeypatch) -> None:
    """Privacy guard: real-Boris fixtures load ONLY when the env flag
    is true. Without it, _load_private_fixtures must return empty —
    even if the dir exists on disk."""
    monkeypatch.delenv("SREDA_PLANNER_BORIS_FIXTURES_ENABLED", raising=False)
    assert _load_private_fixtures() == []


def test_snapshot_private_dir_skipped_when_dir_missing(
    monkeypatch, tmp_path,
) -> None:
    """Codex B.6 R1 MEDIUM fix: monkeypatch _private_fixture_dir to
    deterministically return a non-existent path. Earlier version
    only set env flag and assumed CI never had private/ — but a dev
    checkout with private/ would have leaked through."""
    monkeypatch.setenv("SREDA_PLANNER_BORIS_FIXTURES_ENABLED", "true")
    import sys
    snap_mod = sys.modules[__name__]

    monkeypatch.setattr(
        snap_mod, "_private_fixture_dir",
        lambda: tmp_path / "definitely_not_a_real_dir",
    )
    assert snap_mod._load_private_fixtures() == []


def test_snapshot_private_dir_loads_when_present(
    monkeypatch, tmp_path,
) -> None:
    """Positive coverage: when the dir exists and contains a valid
    fixture file, it's loaded."""
    monkeypatch.setenv("SREDA_PLANNER_BORIS_FIXTURES_ENABLED", "true")
    fake_dir = tmp_path / "boris_real"
    fake_dir.mkdir()
    sample_fixture = {
        "user_message": "купи молоко",
        "context_brief": "",
        "expected_plan": {"schema_version": 1},
    }
    (fake_dir / "sample.json").write_text(
        json.dumps(sample_fixture, ensure_ascii=False),
        encoding="utf-8",
    )

    import sys
    snap_mod = sys.modules[__name__]

    monkeypatch.setattr(snap_mod, "_private_fixture_dir", lambda: fake_dir)

    loaded = snap_mod._load_private_fixtures()
    assert len(loaded) == 1
    label, payload = loaded[0]
    assert label == "boris_real:sample"
    assert payload["user_message"] == "купи молоко"


def test_snapshot_private_dir_raises_on_malformed_json(
    monkeypatch, tmp_path,
) -> None:
    """Codex B.6 R1 MEDIUM fix: malformed real fixtures fail loudly,
    not silently. A partial corpus must not look green."""
    monkeypatch.setenv("SREDA_PLANNER_BORIS_FIXTURES_ENABLED", "true")
    fake_dir = tmp_path / "boris_real"
    fake_dir.mkdir()
    (fake_dir / "broken.json").write_text("{ not valid json", encoding="utf-8")

    import sys
    snap_mod = sys.modules[__name__]

    monkeypatch.setattr(snap_mod, "_private_fixture_dir", lambda: fake_dir)

    with pytest.raises(snap_mod.PrivateFixtureError, match="broken.json"):
        snap_mod._load_private_fixtures()


def test_snapshot_private_dir_raises_on_missing_keys(
    monkeypatch, tmp_path,
) -> None:
    """Missing required keys → loud error."""
    monkeypatch.setenv("SREDA_PLANNER_BORIS_FIXTURES_ENABLED", "true")
    fake_dir = tmp_path / "boris_real"
    fake_dir.mkdir()
    (fake_dir / "incomplete.json").write_text(
        json.dumps({"user_message": "x"}),  # no expected_plan
        encoding="utf-8",
    )

    import sys
    snap_mod = sys.modules[__name__]

    monkeypatch.setattr(snap_mod, "_private_fixture_dir", lambda: fake_dir)

    with pytest.raises(snap_mod.PrivateFixtureError, match="missing required keys"):
        snap_mod._load_private_fixtures()
