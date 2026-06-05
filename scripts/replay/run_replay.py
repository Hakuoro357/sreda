"""Offline-replay harness — Stage 1/2 orchestration (#87).

TODO (Phase B — requires the frozen gold-label manifest + real planner LLM):
    This module orchestrates the replay per ``plans/offline-replay-harness-final.md``
    (the dual-NSC plan). It is INTENTIONALLY a stub in Phase A.

    Phase A delivered the pure, offline-testable pieces ONLY:
      - manifest.py   — ReplayCase / expectations gold-label schema (§4.4)
      - domain_map.py — frozen tool→domain-action map (#4 grading)
      - invariants.py — the 6 objective invariant checkers (§8)
      - mock_tools.py — ReplayWriteRecorder + replay-actual + fail-fast guard (§2)
      - scoring.py    — pre-registered ORDERED go/no-go evaluator (§12)

    Phase B (this module) wires them into the real pipeline:
      reconstruct (reconstruct.py) → build_prompt → call_planner (real MiMo) →
      validate_plan → compile → execute_plan (tools_by_name = ReplayWriteRecorder,
      now_fn = historical now) → compose → build TurnRecord →
      invariants.check_all_invariants(record, frozen gold expectations) →
      scoring.evaluate(...) → ordered verdict + 3 reports (§3).

    IMPORTANT — implement the FINALIZED design, NOT old-vs-new reply similarity:
      - Grade against the FROZEN gold-label manifest (§4.4), never model-derived labels.
      - Stage 2 (execute_plan + compose rendered output) is MANDATORY for the verdict.
      - replay-actual mocks only; missing old trace ⟹ `unknown`, excluded from headline.
      - Pre-registered ORDERED verdict: Stage 0 NO DECISION / Stage 1 NO-GO / Stage 2 GO.
      - Numeric budget caps (§14): refuse to start if dry-run estimate exceeds caps.

    Safety (§2) — enforce BEFORE any call:
      - assert REPLAY_MODE=1; refuse if prod write env loaded.
      - tools_by_name injects ReplayWriteRecorder for ALL write tools; a fail-fast
        guard raises ReplayWriteAttempted if any un-mocked/real write adapter is reached.
      - network allowlist = LLM provider only; DB read-only role for reconstruction.
      - retry ≤2 on 5xx/timeout only (NOT 4xx); cache raw planner/composer outputs.

Dependencies (Phase B only):
    .reconstruct (PromptBuildArgs from quarantine export)
    sreda.runtime.planner.prompt_builder.build_prompt
    sreda.runtime.planner.llm.call_planner   (real MiMo — gated, capped, cached)
    sreda.runtime.planner.validator.validate_plan
    sreda.runtime.planner.executor.execute_plan  (tools_by_name=recorder, now_fn=historical)
    sreda.services.composer.compose.compose
    .invariants.check_all_invariants ; .scoring.evaluate
"""

# Phase A: no implementation — stub only (no prod / no network / no LLM calls).

raise NotImplementedError(
    "run_replay.py is Phase B — it orchestrates the real planner LLM + the frozen "
    "gold-label manifest per plans/offline-replay-harness-final.md. Phase A ships only "
    "the pure modules (manifest/domain_map/invariants/mock_tools/scoring) + their unit "
    "tests. Do not run during Phase A."
)
