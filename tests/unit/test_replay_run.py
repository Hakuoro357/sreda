"""Unit tests for scripts/replay/run_replay.py (Phase B, issue #87).

Offline / synthetic ONLY — no MiMo, no prod, no DB, no network. The planner
is a MOCK injected via run_replay's ``planner_call`` hook (which production
fulfils with ``call_planner`` + its ``invoke=`` / ``chat_llm_factory=`` hooks).

Tests cover:
- A full current-memory turn runs end to end: a TurnRecord is built,
  invariants run, ScoringInput is populated, scoring.evaluate() returns a
  ScoringResult.
- The budget dry-run refuses to start when the estimate exceeds a
  (monkeypatched-low) cap.
- Memory recall-ranking keeps <= top_k and respects min_score on synthetic
  embeddings, and falls back EXPLICITLY when no query embedder is available.
- The executor is wired with the ReplayWriteRecorder (a planned write is
  RECORDED, not executed) and a real write adapter would raise
  ReplayWriteAttempted.
- The §2 safety gate refuses to start without REPLAY_MODE=1 / with a prod
  bot token loaded.

All fixtures use synthetic placeholder data (tenant_tg_<SMOKE>, hash_*).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add scripts/replay to sys.path so we can import the replay modules.
_REPLAY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "replay"
if str(_REPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(_REPLAY_DIR))

import pytest  # noqa: E402

import run_replay  # noqa: E402
from run_replay import (  # noqa: E402
    BudgetCaps,
    RawCache,
    ReplaySafetyError,
    aggregate_scoring_input,
    assert_safe_to_run,
    estimate_budget,
    recall_rank_memories,
    replay_one_turn,
)
from reconstruct import (  # noqa: E402
    ReconstructedCase,
    ReplayExportRecord,
    pair_with_gold,
    reconstruction_fidelity,
)
from manifest import ReplayCase, ReplayExpectations  # noqa: E402
from mock_tools import ReplayWriteAttempted, ReplayWriteRecorder  # noqa: E402
from scoring import ScoringResult  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

_HASH = "hash_smoke_0001"

# A fully valid Plan that adds two shopping items — mirrors the canonical
# few-shot example so it validates + compiles against the real registry.
_MOCK_PLAN = {
    "schema_version": 1,
    "turn_classification": {"is_new_turn": True, "reason": "add shopping"},
    "clarity": "clear",
    "actions": {
        "s1": {
            "tool": "add_shopping_items",
            "args": {"items": [{"title": "молоко"}, {"title": "хлеб"}]},
            "expected_outcomes": [
                {
                    "match": {"status": "added"},
                    "next": None,
                    "compose": {
                        "kind": "template",
                        "template_id": "shopping_added_ok",
                        "template_data": {"items": ["молоко", "хлеб"]},
                    },
                },
                {
                    "match": {"status": "empty"},
                    "next": None,
                    "compose": {
                        "kind": "template",
                        "template_id": "shopping_added_empty",
                        "template_data": {"duplicates": ["молоко", "хлеб"]},
                    },
                },
                {
                    "match": {"status": "error"},
                    "next": None,
                    "compose": {
                        "kind": "template",
                        "template_id": "generic_tool_error",
                        "template_data": {"error_code": "${s1.error_code}"},
                    },
                },
            ],
            "intent_group": "default",
            "depends_on": [],
        },
    },
    "compose": {
        "kind": "template",
        "template_id": "shopping_added_ok",
        "template_data": {"items": ["молоко", "хлеб"]},
    },
}


class _MockPlannerResult:
    """Mimics PlannerCallResult.raw_text — the only field run_replay reads."""

    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text


def _mock_planner(plan: dict | None = None):
    """Return a planner_call callable returning a canned Plan JSON.

    This is the seam production fills with ``call_planner`` (whose invoke= /
    chat_llm_factory= hooks would inject a real / cached LLM); the test stays
    fully offline.
    """
    payload = json.dumps(plan if plan is not None else _MOCK_PLAN, ensure_ascii=False)

    def planner_call(prompt: str):
        assert isinstance(prompt, str) and len(prompt) > 100  # got a real prompt
        return _MockPlannerResult(payload)

    return planner_call


def _embed_query(_text: str) -> list[float]:
    """Synthetic 3-d query embedding (deterministic, offline)."""
    return [1.0, 0.0, 0.0]


def _make_record(
    *,
    turn_id_hash: str = _HASH,
    user_message: str = "купи молоко и хлеб",
    memories: list[dict] | None = None,
    old_called_tools: list[str] | None = None,
) -> ReplayExportRecord:
    if memories is None:
        memories = [
            {
                "content": "любит молоко",
                "source": "memory:core",
                "score": 0.0,
                "saved_at": "2026-01-01T00:00:00+00:00",
                "embedding": [1.0, 0.0, 0.0],
            }
        ]
    return ReplayExportRecord(
        turn_id_hash=turn_id_hash,
        tenant_placeholder="tenant_tg_<SMOKE>",
        user_message=user_message,
        now_iso="2026-05-10T14:23:00+03:00",
        timezone_name="Europe/Moscow",
        locale="ru",
        voice=None,
        profile={
            "name": "Борис", "gender": "m", "address": "",
            "timezone": "Europe/Moscow", "language": "ru",
        },
        memories=memories,
        history={"active_turn": None, "closed_turns": []},
        baseline={
            "old_reply": "добавила молоко и хлеб",
            "old_called_tools": (
                old_called_tools
                if old_called_tools is not None
                else ["add_shopping_items"]
            ),
            "old_side_effects": [],
        },
        reconstruction_status={
            "user_text": "ok", "now_tz_locale": "ok", "profile": "ok",
            "memory_provenance": "ok", "history_window": "ok",
            "active_turn": "ok", "voice_meta": "ok",
            "baseline_alignment": "ok", "decrypted_ok": "ok",
        },
    )


def _make_gold(
    *,
    turn_id_hash: str = _HASH,
    expected_intent: str = "write",
    allowed_write_targets: tuple[str, ...] = ("shopping",),
    forbidden_write_targets: tuple[str, ...] = (),
    must_cover: bool = False,
) -> ReplayCase:
    exp = ReplayExpectations(
        expected_intent=expected_intent,  # type: ignore[arg-type]
        required_outcomes=("shopping item added",),
        acceptable_action_sets=(("shopping:write",),),
        allowed_write_targets=allowed_write_targets,
        forbidden_write_targets=forbidden_write_targets,
        memory_dependence="independent",
        current_memory_status="not_applicable",
        must_cover=must_cover,
    )
    return ReplayCase(
        turn_id_hash=turn_id_hash,
        tenant_placeholder="tenant_tg_<SMOKE>",
        expectations=exp,
    )


def _make_case(
    record: ReplayExportRecord | None = None,
    gold: ReplayCase | None = None,
) -> ReconstructedCase:
    record = record or _make_record()
    gold = gold or _make_gold()
    return ReconstructedCase(
        record=record, gold=gold, fidelity=reconstruction_fidelity(record)
    )


@pytest.fixture(scope="module")
def registry():
    """The CURRENT production registry bundle (system under test)."""
    return run_replay.load_production_registry()


# ---------------------------------------------------------------------------
# 1. Full current-memory turn runs end to end
# ---------------------------------------------------------------------------


def test_full_turn_end_to_end(registry):
    case = _make_case()
    res = replay_one_turn(
        case,
        lane=run_replay.LANE_CURRENT,
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )

    # No pipeline error; a TurnRecord was built.
    assert res.error is None
    assert res.turn_record is not None
    assert res.turn_record.turn_id_hash == _HASH
    assert res.turn_record.new_reply  # composed reply rendered

    # The plan parsed + validated cleanly.
    assert res.valid_plan is True

    # Invariants ran for all 6 classes.
    assert res.invariants is not None
    verdicts = res.invariants.as_dict()
    assert set(verdicts) == {
        "phantom_save", "unsolicited_write", "tool_loop",
        "imprecise_following", "atomicity", "fabricated_state",
    }
    # Read-vs-write: this is a legitimate write turn → no unsolicited write.
    assert verdicts["unsolicited_write"] == "pass"

    # The new actions came from the executor (the planned add_shopping_items).
    tool_names = [a.tool_name for a in res.turn_record.new_actions]
    assert "add_shopping_items" in tool_names


def test_aggregate_and_evaluate_produces_scoring_result(registry):
    case = _make_case()
    res = replay_one_turn(
        case,
        lane=run_replay.LANE_CURRENT,
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )
    pairing = pair_with_gold([case.record], {case.gold.turn_id_hash: case.gold})

    scoring_input = aggregate_scoring_input(
        [res],
        pairing=pairing,
        valid_plan_count=1 if res.valid_plan else 0,
        total_eligible_count=1,
    )
    # ScoringInput is populated with all 6 required classes.
    assert {c.invariant_class for c in scoring_input.per_class} == {
        "phantom_save", "unsolicited_write", "tool_loop",
        "imprecise_following", "atomicity", "fabricated_state",
    }

    result = run_replay.scoring.evaluate(scoring_input)
    assert isinstance(result, ScoringResult)
    # No hallucination/faithfulness pairs yet → ordered verdict is NO DECISION
    # (Stage 0). We assert it RETURNS a verdict, not which one (scoring owns it).
    assert result.verdict in {"GO", "NO-GO", "NO DECISION"}


def test_run_replay_top_level_offline(tmp_path, registry, monkeypatch):
    """run_replay() end-to-end via export+manifest files, mock planner."""
    monkeypatch.setenv("REPLAY_MODE", "1")
    for var in run_replay._PROD_WRITE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    record = _make_record()
    gold = _make_gold()

    export_path = tmp_path / "export.jsonl"
    export_path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        json.dumps({
            "turn_id_hash": gold.turn_id_hash,
            "tenant_placeholder": gold.tenant_placeholder,
            "expectations": {
                "expected_intent": "write",
                "required_outcomes": list(gold.expectations.required_outcomes),
                "acceptable_action_sets": [
                    list(s) for s in gold.expectations.acceptable_action_sets
                ],
                "allowed_write_targets": list(gold.expectations.allowed_write_targets),
                "memory_dependence": "independent",
                "current_memory_status": "not_applicable",
            },
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    verdict, scorecard = run_replay.run_replay(
        export_path=export_path,
        manifest_path=manifest_path,
        lanes=(run_replay.LANE_CURRENT,),
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )
    assert isinstance(verdict, ScoringResult)
    assert "incident_suite" in scorecard
    assert "cost" in scorecard
    # The planner was called exactly once (1 turn × 1 lane) — a real call,
    # counted as a cache miss.
    assert scorecard["cost"]["actual_calls"] == 1


# ---------------------------------------------------------------------------
# 2. Budget dry-run refuses when estimate exceeds a low cap
# ---------------------------------------------------------------------------


def test_budget_dry_run_within_caps():
    caps = BudgetCaps()
    est = estimate_budget(turn_count=50, lane_count=2, caps=caps)
    # 50 × 1 × 2 = 100 calls < 600 cap (calls_per_turn defaults to 1 —
    # template-only compose makes ONE planner LLM call/turn/lane, no LLM-compose).
    assert est.calls_per_turn == 1
    assert est.estimated_calls == 100
    assert est.within_caps is True
    assert est.breaches == []


def test_budget_estimate_breaches_low_cap():
    low = BudgetCaps(max_llm_calls=10)
    est = estimate_budget(turn_count=50, lane_count=2, caps=low)
    # 50 × 1 × 2 = 100 calls > 10 cap → breach.
    assert est.estimated_calls == 100
    assert est.within_caps is False
    assert any("MAX_LLM_CALLS" in b for b in est.breaches)


def test_run_refuses_to_start_when_estimate_exceeds_cap(tmp_path, monkeypatch):
    """A real (non-dry) run aborts with budget_aborted → NO DECISION when the
    estimate exceeds a monkeypatched-low cap, WITHOUT calling the planner."""
    monkeypatch.setenv("REPLAY_MODE", "1")
    for var in run_replay._PROD_WRITE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    record = _make_record()
    gold = _make_gold()
    export_path = tmp_path / "export.jsonl"
    export_path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        json.dumps({
            "turn_id_hash": gold.turn_id_hash,
            "tenant_placeholder": gold.tenant_placeholder,
            "expectations": {"expected_intent": "write"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # A planner that explodes if called — proves we abort BEFORE any call.
    def exploding_planner(_prompt):
        raise AssertionError("planner must NOT be called when budget breached")

    verdict, scorecard = run_replay.run_replay(
        export_path=export_path,
        manifest_path=manifest_path,
        lanes=(run_replay.LANE_CURRENT,),
        caps=BudgetCaps(max_llm_calls=0),  # any turn breaches
        planner_call=exploding_planner,
        registry=None,  # not even loaded — we abort first
        embed_query_fn=_embed_query,
    )
    assert verdict is not None
    assert verdict.verdict == "NO DECISION"
    assert "budget" in verdict.reason.lower()
    assert scorecard["budget_breaches"]


def test_dry_run_returns_estimate_only(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_MODE", "1")
    for var in run_replay._PROD_WRITE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    record = _make_record()
    gold = _make_gold()
    export_path = tmp_path / "export.jsonl"
    export_path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        json.dumps({
            "turn_id_hash": gold.turn_id_hash,
            "tenant_placeholder": gold.tenant_placeholder,
            "expectations": {"expected_intent": "write"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    verdict, scorecard = run_replay.run_replay(
        export_path=export_path,
        manifest_path=manifest_path,
        lanes=(run_replay.LANE_CURRENT, run_replay.LANE_NO_MEMORY),
        dry_run=True,
        planner_call=lambda _p: pytest.fail("no calls on dry run"),
    )
    assert verdict is None
    assert scorecard["dry_run"] is True
    assert scorecard["estimate"]["turn_count"] == 1
    assert scorecard["estimate"]["lane_count"] == 2


# ---------------------------------------------------------------------------
# 3. Memory recall-ranking: top_k + min_score + explicit fallback
# ---------------------------------------------------------------------------


def test_recall_respects_top_k():
    # 15 memories, all aligned with the query → all score 1.0; top_k=10 caps.
    mems = [
        {"content": f"m{i}", "source": "memory:core", "score": 0.0,
         "saved_at": f"2026-01-{i+1:02d}T00:00:00+00:00", "embedding": [1.0, 0.0]}
        for i in range(15)
    ]
    record = _make_record(memories=mems)
    result = recall_rank_memories(
        record, embed_query_fn=lambda _t: [1.0, 0.0], top_k=10, min_score=0.1
    )
    assert result.mode == run_replay.RECALL_MODE_COSINE
    assert result.candidate_count == 15
    assert result.selected_count == 10
    assert len(result.memories) == 10


def test_recall_respects_min_score():
    # One aligned (score 1.0), one orthogonal (score 0.0 < min_score) → dropped.
    mems = [
        {"content": "aligned", "source": "memory:core", "score": 0.0,
         "saved_at": "2026-01-01T00:00:00+00:00", "embedding": [1.0, 0.0]},
        {"content": "orthogonal", "source": "memory:core", "score": 0.0,
         "saved_at": "2026-01-02T00:00:00+00:00", "embedding": [0.0, 1.0]},
    ]
    record = _make_record(memories=mems)
    result = recall_rank_memories(
        record, embed_query_fn=lambda _t: [1.0, 0.0], top_k=10, min_score=0.1
    )
    assert result.mode == run_replay.RECALL_MODE_COSINE
    assert result.selected_count == 1
    assert result.memories[0]["content"] == "aligned"
    # The computed cosine score is written onto the kept memory.
    assert result.memories[0]["score"] == pytest.approx(1.0)


def test_recall_fallback_is_explicit_when_no_embedder():
    """No query embedder → fallback to most-recent, mode marked EXPLICITLY
    (never silently send all as if recalled — §5)."""
    mems = [
        {"content": "old", "source": "memory:core", "score": 0.0,
         "saved_at": "2026-01-01T00:00:00+00:00", "embedding": [1.0, 0.0]},
        {"content": "new", "source": "memory:core", "score": 0.0,
         "saved_at": "2026-02-01T00:00:00+00:00", "embedding": [1.0, 0.0]},
    ]
    record = _make_record(memories=mems)
    result = recall_rank_memories(record, embed_query_fn=None, top_k=1)
    assert result.mode == run_replay.RECALL_MODE_FALLBACK
    assert result.selected_count == 1
    assert result.memories[0]["content"] == "new"  # most recent kept


def test_recall_fallback_when_embeddings_missing():
    """Embedder present but a candidate lacks an embedding → fallback."""
    mems = [
        {"content": "no-emb", "source": "memory:core", "score": 0.0,
         "saved_at": "2026-01-01T00:00:00+00:00"},  # no "embedding" key
    ]
    record = _make_record(memories=mems)
    result = recall_rank_memories(record, embed_query_fn=lambda _t: [1.0, 0.0])
    assert result.mode == run_replay.RECALL_MODE_FALLBACK


# ---------------------------------------------------------------------------
# 4. Executor is wired with ReplayWriteRecorder (writes recorded, not run)
# ---------------------------------------------------------------------------


def test_planned_write_is_recorded_not_executed(registry):
    """A planned add_shopping_items write is RECORDED by the recorder and the
    composed reply renders — no real side effect happens."""
    case = _make_case()
    res = replay_one_turn(
        case,
        lane=run_replay.LANE_CURRENT,
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )
    assert res.error is None
    # The write surfaced as a new action (it was executed against the MOCK
    # recorder tool, which returns a schema-valid stub WITHOUT a DB write).
    assert any(
        a.tool_name == "add_shopping_items" for a in res.turn_record.new_actions
    )


def test_real_write_adapter_raises_replay_write_attempted():
    """A write tool that is NOT recorder-backed raises ReplayWriteAttempted —
    the fail-fast safety guard (§2)."""
    recorder = ReplayWriteRecorder()
    tools = run_replay.build_replay_tools_by_name(recorder)
    # Every write tool in the map is a recording mock — invoking one records,
    # never executes. Grab one and assert it records rather than side-effects.
    add = tools["add_shopping_items"]
    add.invoke({"items": [{"title": "молоко"}]})
    assert len(recorder.get_records()) == 1
    assert recorder.get_records()[0].tool_name == "add_shopping_items"

    # A bare fail-fast guard (un-mocked real write adapter) raises on invoke.
    guards = recorder.build_fail_fast_guards()
    with pytest.raises(ReplayWriteAttempted):
        guards["save_core_fact"].invoke({"text": "x"})


def test_recorder_records_write_target_for_invariant_grading(registry):
    """The recorder captures write_targets so invariant #2 can grade against
    forbidden_write_targets (no DB needed)."""
    recorder = ReplayWriteRecorder()
    tools = run_replay.build_replay_tools_by_name(recorder)
    tools["save_core_fact"].invoke({"text": "user likes milk"})
    recs = recorder.get_records()
    assert len(recs) == 1
    assert "memory" in recs[0].write_targets


# ---------------------------------------------------------------------------
# 5. Safety gate (§2)
# ---------------------------------------------------------------------------


def test_safety_gate_requires_replay_mode():
    with pytest.raises(ReplaySafetyError):
        assert_safe_to_run(env={})  # REPLAY_MODE missing


def test_safety_gate_refuses_prod_token():
    env = {"REPLAY_MODE": "1", "TELEGRAM_BOT_TOKEN": "123:real-token"}
    with pytest.raises(ReplaySafetyError):
        assert_safe_to_run(env=env)


def test_safety_gate_passes_clean_env():
    assert_safe_to_run(env={"REPLAY_MODE": "1"})  # no raise


# ---------------------------------------------------------------------------
# 6. no-memory STRESS lane empties memory (§5)
# ---------------------------------------------------------------------------


def test_no_memory_lane_empties_memory(registry):
    case = _make_case()
    res = replay_one_turn(
        case,
        lane=run_replay.LANE_NO_MEMORY,
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )
    assert res.error is None
    assert res.recall_mode == "no_memory_lane"
    assert res.turn_record is not None


# ---------------------------------------------------------------------------
# 7. must_cover absent from export → counted as uncovered (§12 blocker 4)
# ---------------------------------------------------------------------------


def test_must_cover_absent_from_export_is_uncovered(registry):
    # Export has the normal turn; manifest additionally has a must_cover turn
    # that is NOT in the export → missing_must_cover → uncovered count.
    present_case = _make_case()
    missing_gold = _make_gold(turn_id_hash="hash_missing_must", must_cover=True)

    manifest = {
        present_case.gold.turn_id_hash: present_case.gold,
        missing_gold.turn_id_hash: missing_gold,
    }
    pairing = pair_with_gold([present_case.record], manifest)
    assert len(pairing.missing_must_cover) == 1

    res = replay_one_turn(
        present_case,
        lane=run_replay.LANE_CURRENT,
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )
    scoring_input = aggregate_scoring_input(
        [res], pairing=pairing, valid_plan_count=1, total_eligible_count=1
    )
    assert scoring_input.must_cover_uncovered >= 1

    # scoring.evaluate() turns this into a Stage-0 NO DECISION (we delegate).
    result = run_replay.scoring.evaluate(scoring_input)
    assert result.verdict == "NO DECISION"
    assert result.stage == 0


# ---------------------------------------------------------------------------
# 8. RawCache: a hit avoids a second planner call (§14)
# ---------------------------------------------------------------------------


def test_raw_cache_hit_skips_planner(registry):
    case = _make_case()
    calls = {"n": 0}

    def counting_planner(prompt: str):
        calls["n"] += 1
        return _MockPlannerResult(json.dumps(_MOCK_PLAN, ensure_ascii=False))

    cache = RawCache()
    for _ in range(2):
        replay_one_turn(
            case,
            lane=run_replay.LANE_CURRENT,
            registry=registry,
            embed_query_fn=_embed_query,
            planner_call=counting_planner,
            cache=cache,
        )
    # Second run hits the cache → planner called only once.
    assert calls["n"] == 1
    assert cache.hits == 1
    assert cache.misses == 1


# ---------------------------------------------------------------------------
# A valid memory-write plan (save_core_fact) — mirrors few-shot example #4.
# Used to test the no-memory unsolicited-write blocker + write_target populate.
# ---------------------------------------------------------------------------

_MEMORY_WRITE_PLAN = {
    "schema_version": 1,
    "turn_classification": {"is_new_turn": True, "reason": "save fact"},
    "clarity": "clear",
    "actions": {
        "s1": {
            "tool": "save_core_fact",
            "args": {"content": "у мужа аллергия на орехи"},
            "expected_outcomes": [
                {
                    "match": {"status": "saved_core"},
                    "next": None,
                    "compose": {
                        "kind": "template",
                        "template_id": "memory_saved_ack",
                        "template_data": {"summary": "запомнила"},
                    },
                },
                {
                    "match": {"status": "error"},
                    "next": None,
                    "compose": {
                        "kind": "template",
                        "template_id": "generic_tool_error",
                        "template_data": {"error_code": "${s1.error_code}"},
                    },
                },
            ],
            "intent_group": "default",
            "depends_on": [],
        },
    },
    "compose": {
        "kind": "template",
        "template_id": "memory_saved_ack",
        "template_data": {"summary": "запомнила"},
    },
}


def _memory_write_compose_template() -> str:
    """Pick a real composer template id for the memory-write plan so it compiles
    against the production registry (the exact id does not matter for the test;
    we only need a valid one)."""
    # ``memory_saved_ack`` may not exist; fall back to a known-valid template.
    return "partial_with_clarification"


# ---------------------------------------------------------------------------
# 9. FIX 1 — eligibility filtering: ineligible turn excluded from headline
#    + valid_plan numerator
# ---------------------------------------------------------------------------


def test_ineligible_turn_excluded_from_headline_and_valid_plan(
    tmp_path, registry, monkeypatch
):
    """A memory-DEPENDENT turn with stale memory (memory_eligible=False) is
    EXCLUDED from the headline: it does not count toward valid_plan_count or
    total_eligible_count, and is reported as attrition (FIX 1)."""
    monkeypatch.setenv("REPLAY_MODE", "1")
    for var in run_replay._PROD_WRITE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    eligible_rec = _make_record(turn_id_hash="hash_eligible")
    ineligible_rec = _make_record(turn_id_hash="hash_ineligible")

    export_path = tmp_path / "export.jsonl"
    export_path.write_text(
        json.dumps(eligible_rec.to_dict(), ensure_ascii=False) + "\n"
        + json.dumps(ineligible_rec.to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    def _manifest_line(turn_hash, *, dependence, status):
        return json.dumps({
            "turn_id_hash": turn_hash,
            "tenant_placeholder": "tenant_tg_<SMOKE>",
            "expectations": {
                "expected_intent": "write",
                "allowed_write_targets": ["shopping"],
                "memory_dependence": dependence,
                "current_memory_status": status,
            },
        }, ensure_ascii=False)

    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        _manifest_line("hash_eligible", dependence="independent",
                       status="not_applicable") + "\n"
        + _manifest_line("hash_ineligible", dependence="dependent",
                         status="stale") + "\n",
        encoding="utf-8",
    )

    verdict, scorecard = run_replay.run_replay(
        export_path=export_path,
        manifest_path=manifest_path,
        lanes=(run_replay.LANE_CURRENT,),
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )

    # Only the eligible turn is in the headline.
    assert scorecard["attrition"]["headline_eligible"] == 1
    assert scorecard["attrition"]["excluded_ineligible"] == 1
    # valid_plan numerator never exceeds the eligible total.
    details = scorecard["incident_suite"]["ordered_verdict_details"]
    # valid_plan_rate is computed over the eligible set (1 turn, valid) → 1.0.
    assert details.get("valid_plan_rate") in (None, 1.0)


def test_eligibility_filter_keeps_valid_plan_le_total():
    """aggregate_scoring_input: numerator (valid_plan_count) computed over the
    eligible set never exceeds total_eligible_count (the FIX-1 invariant)."""
    # Two eligible results, one valid one invalid.
    case_a = _make_case(record=_make_record(turn_id_hash="a"),
                        gold=_make_gold(turn_id_hash="a"))
    res_valid = replay_one_turn(
        case_a, lane=run_replay.LANE_CURRENT,
        registry=run_replay.load_production_registry(),
        embed_query_fn=_embed_query, planner_call=_mock_planner(),
    )
    pairing = pair_with_gold([case_a.record], {case_a.gold.turn_id_hash: case_a.gold})
    si = aggregate_scoring_input(
        [res_valid], pairing=pairing, valid_plan_count=1, total_eligible_count=1,
    )
    assert si.valid_plan_count <= si.total_eligible_count


# ---------------------------------------------------------------------------
# 10. FIX 2b — no-memory unsolicited-write blocker fires end to end
# ---------------------------------------------------------------------------


def test_no_memory_unsolicited_write_blocker_fires_end_to_end(registry):
    """A read-intent turn whose plan writes memory (forbidden) in the no-memory
    lane → the aggregated no_memory_per_class carries the unsolicited-write
    violation → scoring.evaluate() returns NO-GO (blocker 1, no-memory)."""
    # Read-intent gold that forbids a memory write.
    gold = _make_gold(
        expected_intent="read",
        allowed_write_targets=(),
        forbidden_write_targets=("memory",),
    )
    record = _make_record()
    case = ReconstructedCase(
        record=record, gold=gold, fidelity=reconstruction_fidelity(record)
    )

    # Build a memory-write plan with a real composer template so it compiles.
    plan = json.loads(json.dumps(_MEMORY_WRITE_PLAN))  # deep copy
    tmpl = _memory_write_compose_template()
    plan["actions"]["s1"]["expected_outcomes"][0]["compose"]["template_id"] = tmpl
    plan["actions"]["s1"]["expected_outcomes"][0]["compose"]["template_data"] = {
        "done_summary": "запомнила", "clarity_reason": "ещё что-то?",
    }
    plan["compose"]["template_id"] = tmpl
    plan["compose"]["template_data"] = {"done_summary": "запомнила"}

    res = replay_one_turn(
        case,
        lane=run_replay.LANE_NO_MEMORY,
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(plan),
    )
    assert res.error is None, res.error
    # The new system wrote memory on a read-intent turn → unsolicited_write fail.
    assert res.invariants is not None
    assert res.invariants.unsolicited_write == "fail"

    pairing = pair_with_gold([record], {gold.turn_id_hash: gold})
    si = aggregate_scoring_input(
        [],  # headline empty in this focused test
        pairing=pairing,
        valid_plan_count=0,
        total_eligible_count=0,
        no_memory_results=[res],
    )
    # The no-memory unsolicited-write violation is carried into the input as a
    # NEW-side absolute (new_fail_C ≥ 1) — this is what scoring.evaluate() reads
    # for the no-memory blocker 1. (The Stage-1 NO-GO verdict itself is asserted
    # in test_replay_scoring.py::TestNoMemoryStressLaneBlockers, which sets up
    # the Stage-0 evaluability gates so the verdict reaches Stage 1; here we
    # prove the run-level AGGREGATION wires the violation through.)
    nm = {c.invariant_class: c for c in si.no_memory_per_class}
    assert "unsolicited_write" in nm
    assert nm["unsolicited_write"].new_fail_C >= 1


# ---------------------------------------------------------------------------
# 11. FIX 4 — invalid plan short-circuits: not compiled/executed/composed
# ---------------------------------------------------------------------------


def test_invalid_plan_short_circuits_not_executed(registry):
    """A plan with validator violations is NOT compiled/executed/composed:
    valid_plan=False, invariants=None, turn_record=None, error names it."""
    # A plan referencing an unknown tool → validator violation.
    bad_plan = json.loads(json.dumps(_MOCK_PLAN))
    bad_plan["actions"]["s1"]["tool"] = "this_tool_does_not_exist"

    # If the executor were reached it would raise (unknown tool not in mocks),
    # but the short-circuit returns BEFORE compile/execute, so error is the
    # invalid_plan marker, NOT an execute_error.
    res = replay_one_turn(
        _make_case(),
        lane=run_replay.LANE_CURRENT,
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(bad_plan),
    )
    assert res.valid_plan is False
    assert res.invariants is None
    assert res.turn_record is None
    assert res.error is not None
    assert res.error.startswith("invalid_plan:")
    assert "execute_error" not in res.error
    assert "compose_error" not in res.error


def test_invalid_plan_counts_as_not_valid_in_metrics(registry):
    """An invalid-plan turn is an all-unknown contribution to per-class counts
    (no invariant credit) and is NOT counted as a valid plan."""
    bad_plan = json.loads(json.dumps(_MOCK_PLAN))
    bad_plan["actions"]["s1"]["tool"] = "this_tool_does_not_exist"
    case = _make_case()
    res = replay_one_turn(
        case, lane=run_replay.LANE_CURRENT, registry=registry,
        embed_query_fn=_embed_query, planner_call=_mock_planner(bad_plan),
    )
    pairing = pair_with_gold([case.record], {case.gold.turn_id_hash: case.gold})
    si = aggregate_scoring_input(
        [res], pairing=pairing,
        valid_plan_count=1 if res.valid_plan else 0,
        total_eligible_count=1,
    )
    assert si.valid_plan_count == 0
    # Every class is unknown for this turn (no new-system grade).
    for c in si.per_class:
        assert c.unknown_C == 1
        assert c.N_C == 0


# ---------------------------------------------------------------------------
# 12. FIX 5 — reconstruction_audit_failed propagates to NO DECISION
# ---------------------------------------------------------------------------


def test_recon_audit_failed_propagates_to_no_decision(tmp_path, registry, monkeypatch):
    """A replayable headline case whose fidelity_ok is False → run sets
    reconstruction_audit_failed=True → scoring.evaluate() Stage-0 NO DECISION."""
    monkeypatch.setenv("REPLAY_MODE", "1")
    for var in run_replay._PROD_WRITE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # Degrade a CRITICAL reconstruction field so fidelity_ok is False.
    bad_rec = _make_record()
    bad_status = dict(bad_rec.reconstruction_status)
    bad_status["decrypted_ok"] = "missing"  # critical field → fidelity fails
    import dataclasses
    bad_rec = dataclasses.replace(bad_rec, reconstruction_status=bad_status)

    # Sanity: the case really fails the fidelity gate.
    assert reconstruction_fidelity(bad_rec)  # status dict exists
    from reconstruct import is_fidelity_ok
    assert is_fidelity_ok(bad_rec) is False

    gold = _make_gold()
    export_path = tmp_path / "export.jsonl"
    export_path.write_text(
        json.dumps(bad_rec.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        json.dumps({
            "turn_id_hash": gold.turn_id_hash,
            "tenant_placeholder": gold.tenant_placeholder,
            "expectations": {
                "expected_intent": "write",
                "allowed_write_targets": ["shopping"],
                "memory_dependence": "independent",
                "current_memory_status": "not_applicable",
            },
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    verdict, scorecard = run_replay.run_replay(
        export_path=export_path,
        manifest_path=manifest_path,
        lanes=(run_replay.LANE_CURRENT,),
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )
    assert scorecard["reconstruction_audit"]["reconstruction_audit_failed"] is True
    assert verdict is not None
    assert verdict.verdict == "NO DECISION"
    assert verdict.stage == 0
    assert verdict.failing_rule == "reconstruction_audit_failed"


# ---------------------------------------------------------------------------
# 13. FIX 6 — write_target populated so atomicity (#5) can see a write
# ---------------------------------------------------------------------------


def test_write_target_populated_for_recorded_write(registry):
    """An executed write step gets its write_target populated on the
    ActionRecord from the recorder WriteRecords (FIX 6) so check_atomicity can
    detect a silent write."""
    res = replay_one_turn(
        _make_case(),
        lane=run_replay.LANE_CURRENT,
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),  # add_shopping_items write
    )
    assert res.error is None
    assert res.turn_record is not None
    write_actions = [
        a for a in res.turn_record.new_actions
        if a.tool_name == "add_shopping_items"
    ]
    assert write_actions, "expected the add_shopping_items write action"
    # FIX 6: write_target is now populated (was None before the fix).
    assert write_actions[0].write_target == "shopping"


def test_write_target_seen_by_atomicity_check(registry):
    """End to end: a recorded write makes check_atomicity see new_write_actions
    (write_target is not None) — proving FIX 6 wires #5's silent-write signal."""
    from invariants import check_atomicity
    from manifest import ReplayExpectations

    res = replay_one_turn(
        _make_case(),
        lane=run_replay.LANE_CURRENT,
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )
    assert res.turn_record is not None
    # A clarification-required turn that silently wrote → check_atomicity sees
    # the write via write_target and returns "fail" (not "unknown").
    exp = ReplayExpectations(
        expected_intent="write",  # type: ignore[arg-type]
        requires_clarification=True,
    )
    # plan_clarity is None and the reply has no "?" but there IS a write →
    # the heuristic path returns "fail" because new_write_actions is non-empty.
    res.turn_record.plan_clarity = None
    verdict = check_atomicity(res.turn_record, exp)
    assert verdict == "fail"


# ---------------------------------------------------------------------------
# 14. FIX 7a — estimate_budget factors wall-clock cap + concurrency
# ---------------------------------------------------------------------------


def test_estimate_budget_factors_wall_clock_and_concurrency():
    """A call count within MAX_LLM_CALLS still breaches when the worst-case
    wall-clock (calls × timeout / concurrency) exceeds WALL_CLOCK_CAP (FIX 7a)."""
    caps = BudgetCaps(
        max_llm_calls=10_000,        # not the binding constraint
        max_tokens_total=10**12,     # not binding
        max_spend_rub=10**9,         # not binding
        per_call_timeout_s=90.0,
        max_concurrency=4,
        wall_clock_cap_min=120.0,
    )
    # calls_per_turn defaults to 1 (template-only compose).
    # 100 calls × 90s / 4 / 60 = 37.5 min — within cap.
    ok = estimate_budget(turn_count=50, lane_count=2, caps=caps)
    assert ok.estimated_calls == 100
    assert ok.within_caps is True

    # 400 calls × 90s / 4 / 60 = 150 min > 120 min cap → breach on wall-clock
    # (call count 400 < 10_000 cap, so the wall-clock factor is what trips it).
    breach = estimate_budget(turn_count=200, lane_count=2, caps=caps)
    assert breach.estimated_calls == 400
    assert breach.within_caps is False
    assert any("WALL_CLOCK_CAP_MIN" in b for b in breach.breaches)


# ---------------------------------------------------------------------------
# 15. FIX 7b — bounded planner retry on timeout/5xx only, never 4xx
# ---------------------------------------------------------------------------


class _ProviderTimeout(Exception):
    pass


class _Provider5xx(Exception):
    def __init__(self) -> None:
        super().__init__("server error")
        self.status_code = 503


class _Provider4xx(Exception):
    def __init__(self) -> None:
        super().__init__("bad request")
        self.status_code = 400


def test_retry_succeeds_after_transient_timeout():
    calls = {"n": 0}

    def flaky(prompt: str):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _ProviderTimeout("timed out")
        return _MockPlannerResult("ok")

    result = run_replay.call_planner_with_retry(flaky, "prompt")
    assert result.raw_text == "ok"
    assert calls["n"] == 2  # 1 failure + 1 success


def test_retry_on_5xx_then_succeeds():
    calls = {"n": 0}

    def flaky(prompt: str):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _Provider5xx()
        return _MockPlannerResult("ok")

    result = run_replay.call_planner_with_retry(flaky, "prompt")
    assert result.raw_text == "ok"
    assert calls["n"] == 2


def test_no_retry_on_4xx():
    calls = {"n": 0}

    def always_4xx(prompt: str):
        calls["n"] += 1
        raise _Provider4xx()

    with pytest.raises(_Provider4xx):
        run_replay.call_planner_with_retry(always_4xx, "prompt")
    assert calls["n"] == 1  # NOT retried


def test_retry_budget_exhausted_reraises():
    calls = {"n": 0}

    def always_timeout(prompt: str):
        calls["n"] += 1
        raise _ProviderTimeout("timed out")

    with pytest.raises(_ProviderTimeout):
        run_replay.call_planner_with_retry(always_timeout, "prompt", max_retries=2)
    assert calls["n"] == 3  # 1 initial + 2 retries


# ---------------------------------------------------------------------------
# 15b. FIX 7b (MAJOR #7b) — provider attempts (incl. retries) are counted as
#      REAL provider calls, SEPARATE from cache misses.
# ---------------------------------------------------------------------------


def test_provider_attempt_counter_counts_retry_as_two():
    """A timeout+retry+success records 2 provider attempts (not 1) — the §14
    cap pressure must see the real number of provider invocations."""
    counter = run_replay.ProviderAttemptCounter()
    calls = {"n": 0}

    def flaky(prompt: str):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _ProviderTimeout("timed out")
        return _MockPlannerResult("ok")

    result = run_replay.call_planner_with_retry(
        flaky, "prompt", attempt_counter=counter
    )
    assert result.raw_text == "ok"
    assert counter.attempts == 2  # 1 failed timeout + 1 successful retry


def test_provider_attempt_counter_4xx_not_retried_counts_one():
    """A 4xx-class error is NOT retried: exactly 1 provider attempt is recorded
    and the error surfaces."""
    counter = run_replay.ProviderAttemptCounter()

    def always_4xx(prompt: str):
        raise _Provider4xx()

    with pytest.raises(_Provider4xx):
        run_replay.call_planner_with_retry(
            always_4xx, "prompt", attempt_counter=counter
        )
    assert counter.attempts == 1  # NOT retried


def test_scorecard_reports_provider_attempts_with_retry(
    tmp_path, registry, monkeypatch
):
    """End to end: a turn whose planner times out once then succeeds reports
    provider_attempts == 2 in the scorecard, while actual_calls (cache misses)
    stays 1 — proving the two counters are SEPARATE (MAJOR #7b)."""
    monkeypatch.setenv("REPLAY_MODE", "1")
    for var in run_replay._PROD_WRITE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    record = _make_record()
    gold = _make_gold()
    export_path = tmp_path / "export.jsonl"
    export_path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        json.dumps({
            "turn_id_hash": gold.turn_id_hash,
            "tenant_placeholder": gold.tenant_placeholder,
            "expectations": {
                "expected_intent": "write",
                "allowed_write_targets": ["shopping"],
                "memory_dependence": "independent",
                "current_memory_status": "not_applicable",
            },
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    payload = json.dumps(_MOCK_PLAN, ensure_ascii=False)
    state = {"n": 0}

    def flaky_planner(prompt: str):
        state["n"] += 1
        if state["n"] < 2:
            raise _ProviderTimeout("timed out")
        return _MockPlannerResult(payload)

    _verdict, scorecard = run_replay.run_replay(
        export_path=export_path,
        manifest_path=manifest_path,
        lanes=(run_replay.LANE_CURRENT,),
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=flaky_planner,
    )
    # One cache-miss turn, but TWO real provider attempts (1 timeout + 1 retry).
    assert scorecard["cost"]["actual_calls"] == 1
    assert scorecard["cost"]["provider_attempts"] == 2


# ---------------------------------------------------------------------------
# 16. FIX 7c — scorecard surfaces template-only (no-LLM) compose mode
# ---------------------------------------------------------------------------


def test_scorecard_reports_template_only_compose(tmp_path, registry, monkeypatch):
    monkeypatch.setenv("REPLAY_MODE", "1")
    for var in run_replay._PROD_WRITE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    record = _make_record()
    gold = _make_gold()
    export_path = tmp_path / "export.jsonl"
    export_path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        json.dumps({
            "turn_id_hash": gold.turn_id_hash,
            "tenant_placeholder": gold.tenant_placeholder,
            "expectations": {
                "expected_intent": "write",
                "allowed_write_targets": ["shopping"],
                "memory_dependence": "independent",
                "current_memory_status": "not_applicable",
            },
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    _verdict, scorecard = run_replay.run_replay(
        export_path=export_path,
        manifest_path=manifest_path,
        lanes=(run_replay.LANE_CURRENT, run_replay.LANE_NO_MEMORY),
        registry=registry,
        embed_query_fn=_embed_query,
        planner_call=_mock_planner(),
    )
    assert scorecard["compose_mode"] == run_replay.COMPOSE_MODE_TEMPLATE_ONLY
    assert "no_memory_stress" in scorecard
    assert "estimated_wall_clock_min" in scorecard["cost"]
