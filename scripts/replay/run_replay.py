"""Offline-replay harness — Stage 1/2 orchestration (#87, Phase B).

Implements the FINALIZED invariant-first design in
``plans/offline-replay-harness-final.md`` (draft-r6). This module WIRES the
pure Phase-A modules (manifest / domain_map / invariants / mock_tools /
scoring) and the Phase-B reconstruct module into the real plan-execute stack:

    reconstruct (load export + pair with frozen gold manifest)
      → recall-rank the exported memories (top_k=10 / min_score=0.1 cosine)
      → build_prompt_args (CURRENT production registry — the thing under test)
      → build_prompt
      → call_planner (real MiMo in prod; a MOCK via call_planner's invoke= /
        chat_llm_factory= hook in tests)
      → json.loads + Plan.model_validate
      → validate_plan (valid-plan := parsed AND zero violations)
      → plan_compiler.compile
      → execute_plan (tools_by_name = ReplayWriteRecorder mocks; historical now)
      → compose (rendered new reply)
      → assemble TurnRecord (new_reply, new_actions, write records)
      → invariants.check_all_invariants(turn, frozen gold expectations)
      → aggregate per-class counts
      → scoring.evaluate() (the ORDERED §12 verdict lives THERE — we only
        populate ScoringInput; we never re-implement the verdict)

NON-GOALS (explicitly rejected designs — do NOT re-introduce):
  * NO "old-vs-new reply similarity" scoring. Grading is invariant-first,
    against the FROZEN gold-label manifest (§4.4).
  * NO LLM judge for faithfulness — we only PREPARE faithfulness pairs for
    Boris's later blind review (§11).
  * The §12 ordered verdict is NOT re-implemented here — ``scoring.evaluate()``
    owns it.

Safety (§2) — enforced BEFORE any planner call:
  * ``REPLAY_MODE=1`` asserted.
  * Refuse to start if a prod write env (real bot tokens) is loaded.
  * The executor is ALWAYS wired with ``build_replay_tools_by_name(recorder)``
    so every write is RECORDED, never real; a fail-fast ``ReplayWriteAttempted``
    guard already exists in mock_tools for any un-mocked real write adapter.
  * Network is the caller's responsibility (allowlist = LLM provider only);
    this module never opens a DB or a socket itself.

Budget caps (§14): a dry-run estimate of LLM calls / tokens / ₽ is computed
BEFORE starting; the run refuses to start if the estimate exceeds ANY cap.
Raw planner/composer outputs are cached keyed by (turn_id_hash, lane, config)
so reruns/resume are free.

PURE-IMPORT / OFFLINE: importing this module has no side effects. The unit
test (``tests/unit/test_replay_run.py``) injects a MOCK planner; it never
touches MiMo, prod, DB, or the network.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# sys.path wiring — make BOTH the replay-module dir (sibling .py files) and the
# sreda src dir importable, exactly as reconstruct.py / mock_tools.py do.
# ---------------------------------------------------------------------------

_REPLAY_DIR = Path(__file__).resolve().parent
if str(_REPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(_REPLAY_DIR))

_SRC_DIR = _REPLAY_DIR.resolve().parents[1] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# --- replay modules (siblings) ---------------------------------------------
from invariants import (  # noqa: E402
    ActionRecord,
    InvariantResults,
    TurnRecord,
    check_all_invariants,
)
from mock_tools import (  # noqa: E402
    ReplayWriteRecorder,
    build_replay_tools_by_name,
)
from reconstruct import (  # noqa: E402
    PairingResult,
    ReconstructedCase,
    ReplayExportRecord,
    build_prompt_args,
    load_export_records,
    load_manifest,
    pair_with_gold,
)
import scoring  # noqa: E402
from scoring import (  # noqa: E402
    PerClassCounts,
    ScoringInput,
    ScoringResult,
)

# ---------------------------------------------------------------------------
# Lanes (§5)
# ---------------------------------------------------------------------------

LANE_CURRENT = "current"      # current-memory HEADLINE lane
LANE_NO_MEMORY = "no-memory"  # no-memory STRESS lane

_ALL_LANES = (LANE_CURRENT, LANE_NO_MEMORY)

# Per-class invariant attribute → scoring invariant_class name.
_INVARIANT_CLASSES: tuple[str, ...] = (
    "phantom_save",
    "unsolicited_write",
    "tool_loop",
    "imprecise_following",
    "atomicity",
    "fabricated_state",
)


# ---------------------------------------------------------------------------
# Memory recall-ranking (DEFERRED from the exporter — §5 / Codex R2)
# ---------------------------------------------------------------------------

RECALL_TOP_K = 10        # match runtime graph.py:254
RECALL_MIN_SCORE = 0.1   # match runtime graph.py:254

# How memories were selected for the prompt (surfaced in the scorecard).
RECALL_MODE_COSINE = "cosine_top_k"            # genuine runtime-equivalent recall
RECALL_MODE_FALLBACK = "fallback_most_recent"  # no query embedding offline (§5)

# Compose mode (FIX 7c, §14): replay uses DETERMINISTIC template-only composition
# (compose() called WITHOUT llm_composer), so the ONLY LLM call per (turn, lane)
# is the planner. Surfaced in the scorecard so planner-only cost accounting is
# explicit and not silently under-reporting an unmade LLM-compose call.
COMPOSE_MODE_TEMPLATE_ONLY = "template_only_no_llm"

# Effective LLM calls per (turn, lane) for the budget estimate, derived from the
# compose mode (FIX 7a, MAJOR #7a). Template-only compose makes NO LLM-compose
# call, so the ONLY planner LLM call per (turn, lane) is 1 — counting 2 (1 planner
# + 1 composer) over-estimates and falsely trips the §14 caps / budget_aborted.
CALLS_PER_TURN_TEMPLATE_ONLY = 1  # 1 planner call/turn/lane (no LLM-compose)


def calls_per_turn_for_compose_mode(compose_mode: str) -> int:
    """Effective LLM calls per (turn, lane) for the §14 budget estimate.

    The replay pipeline runs exactly ONE planner LLM call per (turn, lane) and
    composes deterministically (template-only, no LLM-compose), so the only
    compose mode we support maps to 1 call. Kept explicit (a named constant +
    accessor, not a magic literal) so the cost accounting stays auditable.
    """
    if compose_mode == COMPOSE_MODE_TEMPLATE_ONLY:
        return CALLS_PER_TURN_TEMPLATE_ONLY
    raise ValueError(f"unknown compose_mode {compose_mode!r}")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity (mirrors ``sreda.services.embeddings.cosine_similarity``).

    Re-implemented locally (a few lines) rather than imported so the recall
    ranking is identical even when the sreda package is unavailable in an
    isolated unit-test environment.
    """
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class RecallResult:
    """Outcome of the offline recall-ranking for one turn."""

    memories: list[dict[str, Any]]
    mode: str
    candidate_count: int
    selected_count: int


def recall_rank_memories(
    record: ReplayExportRecord,
    *,
    embed_query_fn: Callable[[str], list[float]] | None,
    top_k: int = RECALL_TOP_K,
    min_score: float = RECALL_MIN_SCORE,
) -> RecallResult:
    """Reproduce the runtime memory recall offline (§5, Codex R2 deferral).

    The export carries ALL current memories, each WITH an ``embedding`` key.
    We cosine-rank them against the turn's query embedding and keep
    ``top_k`` with ``score >= min_score`` — matching ``recall_with_stats``
    (graph.py:254, top_k=10 / min_score=0.1).

    Fallback (made EXPLICIT, never silent — §5): if no query embedder is
    available offline OR any candidate lacks an ``embedding``, we fall back
    to keeping the most-recent ``top_k`` memories (by ``saved_at`` desc) and
    mark ``mode = RECALL_MODE_FALLBACK`` so the scorecard records that these
    were NOT genuinely recalled.

    The ``score`` field on each returned memory dict is overwritten with the
    computed cosine score (or left as-is on the fallback path).
    """
    candidates = list(record.memories or [])
    candidate_count = len(candidates)
    if candidate_count == 0:
        return RecallResult(memories=[], mode=RECALL_MODE_COSINE,
                            candidate_count=0, selected_count=0)

    embeddings_present = all(
        isinstance(m.get("embedding"), (list, tuple)) and m.get("embedding")
        for m in candidates
    )

    if embed_query_fn is not None and embeddings_present:
        query_vec = list(embed_query_fn(record.user_message))
        scored: list[tuple[float, dict[str, Any]]] = []
        for m in candidates:
            score = _cosine_similarity(query_vec, list(m["embedding"]))
            if score < min_score:
                continue
            ranked = dict(m)
            ranked["score"] = score
            scored.append((score, ranked))
        scored.sort(key=lambda t: t[0], reverse=True)
        selected = [m for _, m in scored[:top_k]]
        return RecallResult(
            memories=selected,
            mode=RECALL_MODE_COSINE,
            candidate_count=candidate_count,
            selected_count=len(selected),
        )

    # Fallback — keep most-recent by saved_at (lexicographic ISO sort desc).
    by_recent = sorted(
        candidates,
        key=lambda m: str(m.get("saved_at", "")),
        reverse=True,
    )
    selected = [dict(m) for m in by_recent[:top_k]]
    return RecallResult(
        memories=selected,
        mode=RECALL_MODE_FALLBACK,
        candidate_count=candidate_count,
        selected_count=len(selected),
    )


# ---------------------------------------------------------------------------
# Production registry sourcing (§6 — the system UNDER TEST)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryBundle:
    """The CURRENT production registry handles needed by build_prompt_args.

    These are the system-under-test inputs (§1/§6): the current tool registry,
    composer template ids / llm-prompt keys, and the rendered few-shot block.
    ``registry_map`` (name → ToolSpec) is reused for validate_plan + compile.
    ``snapshot_hash`` is passed to compose for the registry-race check.
    """

    tool_specs: tuple
    composer_template_ids: tuple[str, ...]
    composer_llm_prompt_keys: tuple[str, ...]
    few_shot_block: str
    registry_map: dict
    snapshot_hash: str


def load_production_registry() -> RegistryBundle:
    """Source the CURRENT production registry via the canonical accessors.

    Accessors (verified at file:line in this PR's wiring map):
      * ``ALL_TOOL_SPECS``           — sreda.services.tool_schemas.specs
      * ``REGISTRY.template_ids()``  — sreda.services.composer.registry
      * ``REGISTRY.snapshot_hash()`` — same
      * ``LLM_PROMPT_REGISTRY.prompt_keys()`` — sreda.services.composer.prompts_registry
      * ``render_few_shot_block(effective_llm_keys=...)`` —
        sreda.runtime.planner.few_shot_examples

    Raises ImportError if the sreda planner package is not importable (e.g.
    not deployed to prod yet — see the phase-B-STATUS coupling note). The
    caller decides how to surface that.
    """
    from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
    from sreda.services.composer.registry import REGISTRY
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
    from sreda.runtime.planner.few_shot_examples import render_few_shot_block

    template_ids = tuple(REGISTRY.template_ids())
    llm_keys = tuple(LLM_PROMPT_REGISTRY.prompt_keys())
    few_shot = render_few_shot_block(effective_llm_keys=llm_keys)
    registry_map = {spec.name: spec for spec in ALL_TOOL_SPECS}
    return RegistryBundle(
        tool_specs=tuple(ALL_TOOL_SPECS),
        composer_template_ids=template_ids,
        composer_llm_prompt_keys=llm_keys,
        few_shot_block=few_shot,
        registry_map=registry_map,
        snapshot_hash=REGISTRY.snapshot_hash(),
    )


# ---------------------------------------------------------------------------
# Safety gates (§2)
# ---------------------------------------------------------------------------

# Env vars whose presence implies a PROD WRITE credential is loaded. If ANY of
# these is set we refuse to start (a replay must never carry a real bot token).
_PROD_WRITE_ENV_VARS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "SREDA_TELEGRAM_BOT_TOKEN",
    "MAX_BOT_TOKEN",
    "SREDA_MAX_BOT_TOKEN",
)


class ReplaySafetyError(RuntimeError):
    """Raised when a §2 hard safety gate is violated (refuse to start)."""


def assert_safe_to_run(env: dict[str, str] | None = None) -> None:
    """Enforce the §2 hard safety gates BEFORE any planner call.

    1. ``REPLAY_MODE=1`` must be set.
    2. No prod write env (real bot token) may be loaded.

    Raises ``ReplaySafetyError`` on any violation. The write-tool fail-fast
    guard (``ReplayWriteAttempted``) is enforced separately by
    ``build_replay_tools_by_name`` — this function covers the env gate only.
    """
    env = os.environ if env is None else env
    if env.get("REPLAY_MODE") != "1":
        raise ReplaySafetyError(
            "REPLAY_MODE=1 is required to run the offline replay harness (§2). "
            f"Got REPLAY_MODE={env.get('REPLAY_MODE')!r}."
        )
    loaded = [name for name in _PROD_WRITE_ENV_VARS if env.get(name)]
    if loaded:
        raise ReplaySafetyError(
            f"Refusing to start: prod write env loaded {loaded} (§2). A replay "
            f"must never carry a real bot token. Unset these before running."
        )


# ---------------------------------------------------------------------------
# Budget caps (§14)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCaps:
    """Pre-registered numeric caps (§14). Dry-run estimate compared against
    ALL before start; refuse to start if the estimate exceeds any."""

    max_llm_calls: int = 600
    max_concurrency: int = 4
    per_call_timeout_s: float = 90.0
    max_tokens_total: int = 3_000_000
    wall_clock_cap_min: float = 120.0
    max_spend_rub: float = 500.0

    # Estimation knobs (tunable starting values).
    avg_tokens_per_call: int = 4_000
    rub_per_1k_tokens: float = 0.20  # MiMo rate placeholder (tune before run)


@dataclass
class BudgetEstimate:
    """Dry-run cost estimate (§14) — compared against ``BudgetCaps``."""

    turn_count: int
    lane_count: int
    calls_per_turn: int  # 1 planner call/turn/lane (template-only compose; no LLM-compose)
    estimated_calls: int
    estimated_tokens: int
    estimated_rub: float
    estimated_wall_clock_min: float = 0.0
    breaches: list[str] = field(default_factory=list)

    @property
    def within_caps(self) -> bool:
        return not self.breaches


def estimate_budget(
    turn_count: int,
    lane_count: int,
    caps: BudgetCaps,
    *,
    calls_per_turn: int = CALLS_PER_TURN_TEMPLATE_ONLY,
) -> BudgetEstimate:
    """Compute the dry-run cost estimate and list any cap breaches (§14).

    Note: ``calls_per_turn`` defaults to 1 — the replay does template-only
    compose (``compose()`` WITHOUT ``llm_composer``), so the ONLY LLM call per
    (turn, lane) is the planner; there is no LLM-compose call to count. Counting
    a composer call here (the old default of 2) over-estimated wall-clock /
    token / ₽ and could falsely trip the §14 caps → spurious ``budget_aborted``
    / NO DECISION. Callers that wire a live LLM-composer must pass the higher
    value explicitly.

    FIX 7a (§14): the refuse-to-start check also factors the WALL-CLOCK cap and
    CONCURRENCY, not just call count. Worst-case wall-clock ≈
    (estimated_calls × per_call_timeout_s) / max_concurrency — i.e. all calls
    take the full timeout and run ``max_concurrency``-wide. If that exceeds
    ``wall_clock_cap_min`` the run refuses to start.
    """
    est_calls = turn_count * lane_count * calls_per_turn
    est_tokens = est_calls * caps.avg_tokens_per_call
    est_rub = (est_tokens / 1000.0) * caps.rub_per_1k_tokens

    # Worst-case wall-clock: every call hits the per-call timeout, run
    # max_concurrency-wide. Guard against a 0/negative concurrency cap.
    concurrency = max(1, caps.max_concurrency)
    est_wall_clock_min = (
        est_calls * caps.per_call_timeout_s / concurrency / 60.0
    )

    breaches: list[str] = []
    if est_calls > caps.max_llm_calls:
        breaches.append(
            f"MAX_LLM_CALLS: {est_calls} > {caps.max_llm_calls}"
        )
    if est_tokens > caps.max_tokens_total:
        breaches.append(
            f"MAX_TOKENS_TOTAL: {est_tokens} > {caps.max_tokens_total}"
        )
    if est_rub > caps.max_spend_rub:
        breaches.append(
            f"MAX_SPEND_RUB: {est_rub:.2f} > {caps.max_spend_rub}"
        )
    if est_wall_clock_min > caps.wall_clock_cap_min:
        breaches.append(
            f"WALL_CLOCK_CAP_MIN: {est_wall_clock_min:.1f} > "
            f"{caps.wall_clock_cap_min} (est_calls={est_calls}, "
            f"per_call_timeout_s={caps.per_call_timeout_s}, "
            f"concurrency={concurrency})"
        )
    return BudgetEstimate(
        turn_count=turn_count,
        lane_count=lane_count,
        calls_per_turn=calls_per_turn,
        estimated_calls=est_calls,
        estimated_tokens=est_tokens,
        estimated_rub=est_rub,
        estimated_wall_clock_min=est_wall_clock_min,
        breaches=breaches,
    )


# ---------------------------------------------------------------------------
# Bounded planner retry wrapper (FIX 7b, §14 / p-001)
# ---------------------------------------------------------------------------

PLANNER_MAX_RETRIES = 2  # ≤2 retries (3 total attempts) on timeout/5xx ONLY


def _is_retryable_provider_error(exc: BaseException) -> bool:
    """Classify whether ``exc`` is a RETRYABLE provider error (§14 / p-001).

    Retry ONLY on timeout or 5xx-class provider errors; NEVER on 4xx (a 4xx is a
    client/contract error — retrying wastes budget and can't succeed).

    Detection is defensive (no hard import of provider exception types so this
    module stays importable without sreda):
      * Timeout — class name contains 'Timeout' (PlannerTimeoutError /
        LLMCallTimeout / httpx.*Timeout / asyncio.TimeoutError-ish).
      * HTTP status — an attached ``status_code`` / ``status`` / ``response.status_code``
        in [500, 600) is retryable; [400, 500) is NOT.
    """
    name = type(exc).__name__
    if "Timeout" in name or "TimedOut" in name:
        return True

    status = (
        getattr(exc, "status_code", None)
        or getattr(exc, "status", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
    )
    if isinstance(status, int):
        if 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False  # 4xx — explicitly NOT retryable
    return False  # unknown error class → do not retry (fail fast)


class ProviderAttemptCounter:
    """Accumulator of REAL provider invocations across a run (FIX 7b, MAJOR #7b).

    ``call_planner_with_retry`` may hit the provider up to ``max_retries + 1``
    times for a single (turn, lane); the §14 cap pressure must be measured
    against those REAL attempts, not against cache misses. This counts every
    provider invocation (each initial call + each retry) so the scorecard can
    surface ``provider_attempts`` SEPARATE from the cache-miss count.
    """

    def __init__(self) -> None:
        self.attempts = 0

    def record(self, n: int = 1) -> None:
        self.attempts += n


def call_planner_with_retry(
    planner_call: Callable[..., Any],
    prompt: str,
    *,
    max_retries: int = PLANNER_MAX_RETRIES,
    attempt_counter: ProviderAttemptCounter | None = None,
) -> Any:
    """Call ``planner_call(prompt)`` with ≤``max_retries`` retries (FIX 7b).

    Retries ONLY on timeout / 5xx-class provider errors (``_is_retryable_provider_error``);
    a 4xx or any non-retryable error propagates immediately. The last retryable
    error is re-raised after the retry budget is exhausted.

    Every provider invocation (the initial call AND each retry) is recorded on
    ``attempt_counter`` if one is passed, so the run can report the REAL number
    of provider calls — a timeout+retry+success counts as 2, not 1 (FIX 7b /
    MAJOR #7b: a cache-miss count of 1 would undercount real spend/attempts).
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):  # 1 initial + max_retries
        if attempt_counter is not None:
            attempt_counter.record()  # a real provider invocation is about to happen
        try:
            return planner_call(prompt)
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise/retry
            if not _is_retryable_provider_error(exc) or attempt >= max_retries:
                raise
            last_exc = exc
    # Unreachable (loop either returns or raises), but keep mypy/readers happy.
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Raw planner/composer output cache (§14)
# ---------------------------------------------------------------------------


class RawCache:
    """In-memory cache of raw planner/composer outputs keyed by
    (turn_id_hash, lane, config) — §14 resume/rerun-free. Cached calls do
    NOT count against the LLM-call cap.

    A trivial dict cache is enough for a single 50-turn pass; a persistent
    backing store (JSONL on disk) is a later optimization (the key shape is
    chosen so it can be serialized as-is).
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str], str] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(turn_id_hash: str, lane: str, config: str) -> tuple[str, str, str]:
        return (turn_id_hash, lane, config)

    def get(self, turn_id_hash: str, lane: str, config: str) -> str | None:
        val = self._store.get(self._key(turn_id_hash, lane, config))
        if val is None:
            self.misses += 1
        else:
            self.hits += 1
        return val

    def put(self, turn_id_hash: str, lane: str, config: str, raw: str) -> None:
        self._store[self._key(turn_id_hash, lane, config)] = raw


# ---------------------------------------------------------------------------
# Per-turn replay (the core wiring)
# ---------------------------------------------------------------------------


@dataclass
class TurnReplayResult:
    """Everything produced for one (turn, lane) replay."""

    turn_id_hash: str
    lane: str
    turn_record: TurnRecord | None
    invariants: InvariantResults | None
    valid_plan: bool
    recall_mode: str
    error: str | None = None


def _step_results_to_actions(
    execution_log: Any,
    *,
    plan: Any = None,
    write_records: list | None = None,
) -> list[ActionRecord]:
    """Map executor ``ExecutionLog.steps`` → invariants ``ActionRecord`` list.

    ``intent_group`` is populated from the plan actions when available (the
    executor's ``StepResult`` does NOT carry it).

    ``write_target`` (FIX 6, §8 #5) is populated for executed WRITE steps from
    the ``ReplayWriteRecorder`` WriteRecords so ``check_atomicity`` (#5) and #2's
    silent-write heuristics can see a write happened. Invariants #1/#2 still grade
    against the authoritative recorder records directly; this ActionRecord signal
    is the convenience path for the atomicity heuristic.

    Matching heuristic (documented): the recorder produces one WriteRecord per
    write-tool ``invoke`` in call order, but ``StepResult`` carries no recorder
    cross-reference, so a precise step↔record id match is not available. We match
    by tool name: a WriteRecord whose ``tool_name`` equals the step's ``tool`` is
    consumed FIFO (first unconsumed record for that tool). Its primary
    ``write_target`` and ``authorized`` flag are copied onto the ActionRecord.
    A step with no matching record keeps ``write_target=None`` (it was a read, or
    no write was recorded for it).
    """
    actions: list[ActionRecord] = []
    if plan is None:
        plan = getattr(execution_log, "plan", None)

    # Group recorder WriteRecords by tool name (preserve call order) so each step
    # consumes the next unconsumed record for its tool (FIFO per tool).
    records_by_tool: dict[str, list] = {}
    for rec in (write_records or []):
        records_by_tool.setdefault(rec.tool_name, []).append(rec)
    consumed: dict[str, int] = {}

    for step in execution_log.steps:
        intent_group = None
        if plan is not None:
            action = plan.actions.get(step.step_id)
            if action is not None:
                intent_group = action.intent_group

        write_target: str | None = None
        authorized = True
        recs = records_by_tool.get(step.tool)
        if recs:
            idx = consumed.get(step.tool, 0)
            if idx < len(recs):
                rec = recs[idx]
                consumed[step.tool] = idx + 1
                write_target = rec.write_target
                authorized = rec.authorized

        actions.append(
            ActionRecord(
                tool_name=step.tool,
                args={},  # resolved args not needed for the invariant set we grade
                raw_output=step.raw_output,
                parsed_status=step.matched_status or step.status,
                write_target=write_target,
                authorized=authorized,
                intent_group=intent_group,
            )
        )
    return actions


def replay_one_turn(
    case: ReconstructedCase,
    *,
    lane: str,
    registry: RegistryBundle,
    embed_query_fn: Callable[[str], list[float]] | None,
    planner_call: Callable[..., Any],
    cache: RawCache | None = None,
    config_key: str = "v1",
    attempt_counter: ProviderAttemptCounter | None = None,
) -> TurnReplayResult:
    """Run ONE turn through the full pipeline for ONE lane.

    Pipeline (§6/§8): recall-rank → build_prompt_args → build_prompt →
    planner_call → parse Plan → validate_plan → compile → execute_plan
    (ReplayWriteRecorder) → compose → TurnRecord → check_all_invariants.

    Parameters
    ----------
    case :
        A paired export record + gold ReplayCase (from ``pair_with_gold``).
        Must have a non-None ``gold`` (the caller skips unmatched cases).
    lane :
        ``LANE_CURRENT`` (current memory) or ``LANE_NO_MEMORY`` (empty memory).
    registry :
        The CURRENT production registry bundle (system under test).
    embed_query_fn :
        Offline query embedder for recall ranking; None → fallback recall.
    planner_call :
        A callable matching ``call_planner``'s public signature — production
        passes ``sreda.runtime.planner.llm.call_planner`` (which itself
        accepts ``invoke=`` / ``chat_llm_factory=`` hooks for caching + the
        test mock). Receives the built prompt and returns a result object
        with a ``.raw_text`` attribute.
    cache :
        Optional raw-output cache (§14). On a hit the planner is NOT called.
    """
    # Local imports of the planner/composer entry points so this module
    # imports cleanly even when sreda is unavailable (the recall/budget/safety
    # paths above + their unit tests do not need sreda).
    from sreda.runtime.planner.prompt_builder import build_prompt
    from sreda.runtime.planner.json_parse import parse_planner_json
    from sreda.runtime.planner.schemas import Plan
    from sreda.runtime.planner.validator import validate_plan
    from sreda.runtime.planner import plan_compiler
    from sreda.runtime.planner.executor import execute_plan
    from sreda.services.composer.compose import compose
    import asyncio

    record = case.record
    gold = case.gold
    assert gold is not None, "replay_one_turn requires a matched gold case"
    expectations = gold.expectations

    # --- memory lane (§5): current = recalled memories; no-memory = empty ---
    if lane == LANE_NO_MEMORY:
        record = _with_empty_memories(record)
        recall_mode = "no_memory_lane"
    else:
        recall = recall_rank_memories(record, embed_query_fn=embed_query_fn)
        record = _with_memories(record, recall.memories)
        recall_mode = recall.mode

    # --- build prompt args from the CURRENT registry (system under test) ----
    prompt_args = build_prompt_args(
        record,
        tool_specs=registry.tool_specs,
        composer_template_ids=registry.composer_template_ids,
        composer_llm_prompt_keys=registry.composer_llm_prompt_keys,
        few_shot_block=registry.few_shot_block,
    )
    prompt = build_prompt(prompt_args)

    # --- planner call (cached) ----------------------------------------------
    raw_text: str | None = None
    if cache is not None:
        raw_text = cache.get(record.turn_id_hash, lane, config_key)
    if raw_text is None:
        # FIX 7b (§14/p-001): bounded retry on timeout/5xx ONLY (never 4xx).
        # Real provider attempts (incl. retries) are accumulated on
        # ``attempt_counter`` so the scorecard reports actual provider spend,
        # SEPARATE from the cache-miss count (MAJOR #7b).
        result = call_planner_with_retry(
            planner_call, prompt, attempt_counter=attempt_counter
        )
        raw_text = result.raw_text
        if cache is not None:
            cache.put(record.turn_id_hash, lane, config_key, raw_text)

    # --- parse + validate ---------------------------------------------------
    valid_plan = False
    try:
        payload = parse_planner_json(raw_text)  # strict envelope-strip (#113)
        plan = Plan.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — any parse/schema error → invalid plan
        return TurnReplayResult(
            turn_id_hash=record.turn_id_hash, lane=lane, turn_record=None,
            invariants=None, valid_plan=False, recall_mode=recall_mode,
            error=f"plan_parse_error: {type(exc).__name__}: {exc}",
        )

    violations = validate_plan(
        plan,
        registry.registry_map,
        composer_template_ids=frozenset(registry.composer_template_ids),
        composer_llm_prompt_keys=frozenset(registry.composer_llm_prompt_keys),
    )
    # valid-plan := parsed AND zero violations. The validator's contract
    # (validator.py:2257 «empty == plan ready to execute») has no severity
    # field on Violation — any violation is execution-blocking, so a clean
    # plan is exactly an empty violations list.
    valid_plan = not violations

    # --- FIX 4 (§6): SHORT-CIRCUIT invalid plans BEFORE compile/execute ------
    # Production (orchestrator.py:499-527) stops on violations BEFORE compile;
    # an invalid plan is never compiled/executed/composed. Mirror that: a turn
    # with violations counts in valid-plan metrics (as not-valid) but earns NO
    # invariant credit (invariants=None → all-unknown in aggregation).
    if violations:
        return TurnReplayResult(
            turn_id_hash=record.turn_id_hash, lane=lane, turn_record=None,
            invariants=None, valid_plan=False, recall_mode=recall_mode,
            error=f"invalid_plan: {len(violations)} violations",
        )

    # --- compile + execute (ReplayWriteRecorder — never real writes) --------
    recorder = ReplayWriteRecorder()
    tools_by_name = build_replay_tools_by_name(recorder)

    historical_now = datetime.fromisoformat(record.now_iso)

    try:
        execution_plan = plan_compiler.compile(plan, registry.registry_map)
        execution_log = asyncio.run(
            execute_plan(
                execution_plan,
                tools_by_name,
                registry.registry_map,
                now_fn=lambda: historical_now,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return TurnReplayResult(
            turn_id_hash=record.turn_id_hash, lane=lane, turn_record=None,
            invariants=None, valid_plan=valid_plan, recall_mode=recall_mode,
            error=f"execute_error: {type(exc).__name__}: {exc}",
        )

    # --- compose the new reply ----------------------------------------------
    # FIX 7c (§14 cost accounting): compose is called WITHOUT ``llm_composer``,
    # i.e. DETERMINISTIC TEMPLATE-ONLY composition — no LLM-compose call is made
    # here. This is the accepted first-pass (wiring a live llm_composer would
    # DOUBLE the LLM cost; per the reviewer, template-only + an explicit report
    # is correct). Consequence: the ONLY LLM call per (turn, lane) is the planner,
    # so the planner-only cost accounting in estimate_budget / the scorecard is
    # accurate and NOT silently under-reporting an unmade compose call. The
    # scorecard surfaces this via ``compose_mode="template_only_no_llm"``.
    try:
        compose_result = compose(
            plan.compose,
            execution_log,
            expected_registry_snapshot_hash=registry.snapshot_hash,
        )
        new_reply = compose_result.text
    except Exception as exc:  # noqa: BLE001
        return TurnReplayResult(
            turn_id_hash=record.turn_id_hash, lane=lane, turn_record=None,
            invariants=None, valid_plan=valid_plan, recall_mode=recall_mode,
            error=f"compose_error: {type(exc).__name__}: {exc}",
        )

    # --- assemble TurnRecord + run invariants -------------------------------
    # FIX 6 (§8 #5): populate write_target on executed write actions from the
    # recorder records so #5 (atomicity) / #2 silent-write heuristics can see
    # them.
    new_actions = _step_results_to_actions(
        execution_log, plan=plan, write_records=recorder.get_records()
    )
    old_actions = _old_actions_from_baseline(record)
    plan_clarity = (
        "needs_clarification"
        if plan.clarity == "needs_clarification"
        else None
    )

    turn = TurnRecord(
        turn_id_hash=record.turn_id_hash,
        user_message=record.user_message,
        new_reply=new_reply,
        old_reply=(record.baseline or {}).get("old_reply"),
        new_actions=new_actions,
        old_actions=old_actions,
        tool_results_this_turn={a.tool_name for a in new_actions},
        plan_clarity=plan_clarity,
        grounding_sources=None,
    )

    invariants = check_all_invariants(
        turn,
        expectations,
        new_write_records=recorder.get_records(),
    )

    return TurnReplayResult(
        turn_id_hash=record.turn_id_hash, lane=lane, turn_record=turn,
        invariants=invariants, valid_plan=valid_plan, recall_mode=recall_mode,
        error=None,
    )


def _with_memories(
    record: ReplayExportRecord, memories: list[dict[str, Any]]
) -> ReplayExportRecord:
    """Return a copy of ``record`` with ``memories`` replaced (frozen dataclass)."""
    import dataclasses
    return dataclasses.replace(record, memories=memories)


def _with_empty_memories(record: ReplayExportRecord) -> ReplayExportRecord:
    """no-memory STRESS lane (§5): empty memories + neutral profile name/address.

    We blank the personalized profile fields but keep timezone/language so the
    historical ``now`` stays correct.
    """
    import dataclasses
    neutral_profile = dict(record.profile)
    neutral_profile["name"] = ""
    neutral_profile["address"] = ""
    return dataclasses.replace(record, memories=[], profile=neutral_profile)


def _old_actions_from_baseline(
    record: ReplayExportRecord,
) -> list[ActionRecord] | None:
    """Build old-actual ActionRecords from the baseline trace (§8 replay-actual).

    MISSING old trace ⟹ return ``None`` (NOT empty list) so tool-dependent
    invariants score ``unknown`` and the turn is EXCLUDED from the headline
    for those classes (§8 — never a neutral mock, never zero).
    """
    baseline = record.baseline or {}
    old_tools = baseline.get("old_called_tools")
    if old_tools is None:
        return None  # missing trace → unknown for tool-dependent classes
    return [
        ActionRecord(tool_name=name, args={}, raw_output=None)
        for name in old_tools
    ]


# ---------------------------------------------------------------------------
# Aggregation → ScoringInput (§8 → §12)
# ---------------------------------------------------------------------------


# No-memory STRESS-lane safety subset (§5/§12 blocker 1+2). Only these classes
# are graded in the stress lane (empty memory changes the task, so personalized /
# memory-save invariants are not headline-graded there — but #1/#2/#6 safety MUST
# hold regardless of memory).
_NO_MEMORY_SAFETY_CLASSES: tuple[str, ...] = (
    "phantom_save",
    "unsolicited_write",
    "fabricated_state",
)


def _tally_per_class(
    results: list[TurnReplayResult],
    classes: tuple[str, ...],
) -> dict[str, PerClassCounts]:
    """Tally §8 per-class old-vs-new counts over ``results`` for ``classes``.

    Shared by the headline and the no-memory stress aggregation. Denominator
    excludes ``unknown`` (either side unknown ⟹ unknown_C).
    """
    per_class: dict[str, PerClassCounts] = {
        cls: PerClassCounts(invariant_class=cls)  # type: ignore[arg-type]
        for cls in classes
    }
    for res in results:
        inv = res.invariants
        if inv is None:
            for cls in classes:
                per_class[cls].unknown_C += 1
            continue
        new_verdicts = inv.as_dict()
        old_verdicts = _old_invariant_verdicts(res)
        for cls in classes:
            new_v = new_verdicts[cls]
            old_v = old_verdicts[cls]
            counts = per_class[cls]
            if new_v == "unknown" or old_v == "unknown":
                counts.unknown_C += 1
            elif old_v == "fail" and new_v == "pass":
                counts.fixed_C += 1
            elif old_v == "pass" and new_v == "fail":
                counts.regression_C += 1
            elif old_v == "pass" and new_v == "pass":
                counts.both_ok_C += 1
            else:  # both fail
                counts.both_fail_C += 1
    return per_class


def _tally_no_memory_safety(
    results: list[TurnReplayResult],
) -> dict[str, PerClassCounts]:
    """Tally the no-memory STRESS lane for the safety subset (FIX 2b, §5/§12).

    Two grading modes per the design:

    * ``unsolicited_write`` (blocker 1 — NEW-side ABSOLUTE, enforceable NOW):
      a new-side ``fail`` (new wrote a forbidden_write_target on a read-intent
      turn) is a violation REGARDLESS of old-side. We record it as ``both_fail_C``
      so ``new_fail_C = regression_C + both_fail_C`` is ≥ 1 and blocker 1 fires.
      This is the whole point of the prompt's note — the unsolicited-write check
      does NOT need an old baseline.

    * ``phantom_save`` / ``fabricated_state`` (blocker 2 — REGRESSION, needs
      old-side): use the standard old-vs-new tally. Until replay-actual old-side
      lands, old is ``unknown`` ⟹ ``unknown_C`` ⟹ regression_C = 0 ⟹ no-ops
      (the wiring is present; it activates once old-side coverage exists).
    """
    per_class: dict[str, PerClassCounts] = {
        cls: PerClassCounts(invariant_class=cls)  # type: ignore[arg-type]
        for cls in _NO_MEMORY_SAFETY_CLASSES
    }
    for res in results:
        inv = res.invariants
        if inv is None:
            for cls in _NO_MEMORY_SAFETY_CLASSES:
                per_class[cls].unknown_C += 1
            continue
        new_verdicts = inv.as_dict()
        old_verdicts = _old_invariant_verdicts(res)

        # --- unsolicited_write: NEW-side absolute -------------------------
        uw = per_class["unsolicited_write"]
        uw_new = new_verdicts["unsolicited_write"]
        if uw_new == "fail":
            uw.both_fail_C += 1   # → new_fail_C ≥ 1 → blocker 1 fires
        elif uw_new == "pass":
            uw.both_ok_C += 1
        else:
            uw.unknown_C += 1

        # --- phantom_save / fabricated_state: old-vs-new regression -------
        for cls in ("phantom_save", "fabricated_state"):
            new_v = new_verdicts[cls]
            old_v = old_verdicts[cls]
            counts = per_class[cls]
            if new_v == "unknown" or old_v == "unknown":
                counts.unknown_C += 1
            elif old_v == "fail" and new_v == "pass":
                counts.fixed_C += 1
            elif old_v == "pass" and new_v == "fail":
                counts.regression_C += 1
            elif old_v == "pass" and new_v == "pass":
                counts.both_ok_C += 1
            else:  # both fail
                counts.both_fail_C += 1
    return per_class


def aggregate_scoring_input(
    headline_results: list[TurnReplayResult],
    *,
    pairing: PairingResult,
    valid_plan_count: int,
    total_eligible_count: int,
    no_memory_results: list[TurnReplayResult] | None = None,
    budget_aborted: bool = False,
    reconstruction_audit_failed: bool = False,
) -> ScoringInput:
    """Build a ``ScoringInput`` from the per-turn headline results (§8).

    Per-class counts follow §8 scoring (denominator excludes ``unknown``):
      fixed_C    = old fails, new passes
      regression = old passes, new fails
      both_ok    = both pass
      both_fail  = both fail
      unknown    = either side unknown (excluded from N_C)

    ``no_memory_results`` (FIX 2b, §5/§12): the no-memory STRESS-lane per-turn
    results. When provided, they are aggregated for the safety subset
    {phantom_save, unsolicited_write, fabricated_state} into
    ``ScoringInput.no_memory_per_class`` so ``scoring.evaluate()`` can apply
    blocker 1 (unsolicited-write > 0 in no-memory → NO-GO) and blocker 2 (safety
    regression in no-memory → NO-GO). ``None`` / empty ⟹ no extra blocking
    (backward-compatible). Blocker 1's unsolicited-write check is a NEW-side
    absolute (computable now); blocker 2 regression needs old-side and no-ops on
    ``unknown`` until replay-actual lands.

    ``must_cover_uncovered`` combines (a) gold must_cover turns ABSENT from the
    export (``pairing.missing_must_cover``) and (b) must_cover turns present but
    whose OLD-side (then NEW-side) coverage is all ``unknown`` (uncovered after
    reconstruction — §8 bias audit / §12 blocker 4; see FIX 3).

    NOTE: the §12 ORDERED verdict is NOT computed here — that is
    ``scoring.evaluate()``'s job. We only populate the input.
    """
    per_class = _tally_per_class(headline_results, _INVARIANT_CLASSES)

    # FIX 2b: no-memory stress lane → safety-subset counts.
    no_memory_per_class: list[PerClassCounts] = []
    if no_memory_results:
        nm = _tally_no_memory_safety(no_memory_results)
        no_memory_per_class = list(nm.values())

    must_cover_uncovered = _count_must_cover_uncovered(headline_results, pairing)

    return ScoringInput(
        per_class=list(per_class.values()),
        no_memory_per_class=no_memory_per_class,
        must_cover_uncovered=must_cover_uncovered,
        valid_plan_count=valid_plan_count,
        total_eligible_count=total_eligible_count,
        # Hallucination + faithfulness pairs are PREPARED separately (§11) and
        # filled by the caller; left empty here means «not evaluable» →
        # scoring.evaluate() returns NO DECISION (Stage 0), which is correct
        # until detector output + Boris's manual review are wired.
        hallucination_pairs=[],
        faithfulness_pairs=[],
        budget_aborted=budget_aborted,
        reconstruction_audit_failed=reconstruction_audit_failed,
    )


def _old_invariant_verdicts(res: TurnReplayResult) -> dict[str, str]:
    """Old-behavior invariant verdicts via replay-actual (§8).

    If the old trace is MISSING (``turn.old_actions is None``), every
    tool-dependent class is ``unknown`` and the turn is excluded from the
    headline for those classes (§8). A full replay-actual re-execution of the
    OLD plan is a separate, heavier piece; until that lands, the honest signal
    is ``unknown`` rather than a fabricated old verdict — which keeps the §12
    fixed/regression math sound (it never invents a baseline).
    """
    turn = res.turn_record
    if turn is None or turn.old_actions is None:
        return {cls: "unknown" for cls in _INVARIANT_CLASSES}
    # Old trace present but old-side invariant grading is the replay-actual
    # piece (DEFERRED — documented). Until then we do NOT fabricate an old
    # verdict; report unknown so the turn is excluded from fixed/regression.
    return {cls: "unknown" for cls in _INVARIANT_CLASSES}


def _count_must_cover_uncovered(
    headline_results: list[TurnReplayResult],
    pairing: PairingResult,
) -> int:
    """Count must_cover turns that are uncovered (§12 blocker 4).

    = (gold must_cover turns absent from the export)
      + (must_cover turns present but all-unknown after reconstruction)
    """
    # (a) must_cover gold absent from the export.
    absent = len(pairing.missing_must_cover)

    # (b) must_cover turns present but uncovered (no invariants ran, OR a
    # reconstruction error, OR all classes unknown). Build a hash→must_cover map.
    must_cover_hashes: set[str] = set()
    for case in pairing.cases:
        if case.gold is not None and case.gold.expectations.must_cover:
            must_cover_hashes.add(case.record.turn_id_hash)

    uncovered_present = 0
    seen: set[str] = set()
    for res in headline_results:
        if res.turn_id_hash not in must_cover_hashes:
            continue
        seen.add(res.turn_id_hash)
        if res.invariants is None:
            uncovered_present += 1
            continue
        # FIX 3 (§8/§12): a must-cover turn is UNCOVERED when its OLD-side
        # replay-actual coverage is absent (old_actions is None ⟹ old-side
        # all-unknown), not only when the NEW-side is all-unknown. Without an
        # old baseline the fixed/regression verdict for the named incident can't
        # be computed, so the turn is uncovered. Use the same old-side signal as
        # aggregation (_old_invariant_verdicts).
        old_verdicts = _old_invariant_verdicts(res)
        if all(v == "unknown" for v in old_verdicts.values()):
            uncovered_present += 1
            continue
        new_verdicts = res.invariants.as_dict()
        if all(v == "unknown" for v in new_verdicts.values()):
            uncovered_present += 1

    # A must_cover turn present in the manifest+export but never replayed at all
    # (e.g. filtered by --limit) is also uncovered.
    not_replayed = len(must_cover_hashes - seen)

    return absent + uncovered_present + not_replayed


# ---------------------------------------------------------------------------
# Scorecard (§3 — the 3 reports + verdict + cost)
# ---------------------------------------------------------------------------


def build_scorecard(
    *,
    verdict: ScoringResult,
    estimate: BudgetEstimate,
    cache: RawCache,
    headline_results: list[TurnReplayResult],
    pairing: PairingResult,
    lanes: tuple[str, ...],
    excluded_ineligible: int = 0,
    no_memory_results: list[TurnReplayResult] | None = None,
    reconstruction_audit_failed: bool = False,
    provider_attempts: int | None = None,
) -> dict[str, Any]:
    """Assemble the redacted scorecard (§3). No raw PII — only structural
    counts, the verdict, and estimated/actual cost (eval-report rule §14).

    The 3 reports (§3):
      1. reconstruction audit — fidelity pass/fail counts.
      2. incident-invariant suite — per-class verdicts + the ordered verdict.
      3. broad stratified regression — (smoke at 50; per-class counts reused).

    Also surfaces (FIX 1/5/7c):
      * ``attrition`` — eligible-filter attrition (ineligible turns excluded
        from the headline) so the headline N is auditable.
      * ``no_memory_stress`` — NEW-side safety-class tally on the no-memory lane.
      * ``compose_mode`` — template-only (no LLM compose), so planner-only cost
        accounting is explicit (FIX 7c).
    """
    actual_calls = cache.misses  # cache hits cost nothing
    recall_modes: dict[str, int] = {}
    errors: list[dict[str, str]] = []
    for res in headline_results:
        recall_modes[res.recall_mode] = recall_modes.get(res.recall_mode, 0) + 1
        if res.error:
            errors.append({"turn": res.turn_id_hash, "lane": res.lane,
                          "error": res.error})
    for res in (no_memory_results or []):
        recall_modes[res.recall_mode] = recall_modes.get(res.recall_mode, 0) + 1
        if res.error:
            errors.append({"turn": res.turn_id_hash, "lane": res.lane,
                          "error": res.error})

    fidelity_ok = sum(1 for c in pairing.cases if c.fidelity_ok)
    fidelity_total = len(pairing.cases)

    return {
        "verdict": verdict.verdict,
        "stage": verdict.stage,
        "reason": verdict.reason,
        "failing_rule": verdict.failing_rule,
        "lanes": list(lanes),
        # Compose mode (FIX 7c) — replay is deterministic template-only, so the
        # only LLM call per (turn, lane) is the planner; cost is planner-only.
        "compose_mode": COMPOSE_MODE_TEMPLATE_ONLY,
        # Report 1 — reconstruction audit.
        "reconstruction_audit": {
            "fidelity_ok": fidelity_ok,
            "fidelity_total": fidelity_total,
            "reconstruction_audit_failed": reconstruction_audit_failed,
            "unmatched_export_records": sum(
                1 for c in pairing.cases if c.unmatched
            ),
            "unmatched_gold": len(pairing.unmatched_gold),
            "missing_must_cover": len(pairing.missing_must_cover),
        },
        # Eligibility attrition (FIX 1) — headline scored only over eligible.
        "attrition": {
            "headline_eligible": len(headline_results),
            "excluded_ineligible": excluded_ineligible,
        },
        # Report 2 — incident-invariant suite (headline).
        "incident_suite": {
            "per_class": {
                c.invariant_class: {
                    "fixed": c.fixed_C,
                    "regression": c.regression_C,
                    "both_ok": c.both_ok_C,
                    "both_fail": c.both_fail_C,
                    "unknown": c.unknown_C,
                }
                for c in verdict.details.get("per_class_snapshot", [])
            }
            or _per_class_snapshot_from_results(headline_results),
            "ordered_verdict_details": verdict.details,
        },
        # No-memory STRESS lane (§5/§12) — NEW-side safety-class tally.
        "no_memory_stress": _per_class_snapshot_from_results(
            no_memory_results or [], classes=_NO_MEMORY_SAFETY_CLASSES
        ),
        # Cost (§14 eval-report rule).
        # NOTE (FIX 7b / MAJOR #7b): the §14 cap pressure is measured against
        # REAL provider attempts (``provider_attempts``, incl. retries), NOT
        # ``actual_calls``/``cache_misses`` (which count distinct cache-miss
        # turns and undercount when a turn timed out and was retried).
        "cost": {
            "estimated_calls": estimate.estimated_calls,
            "estimated_tokens": estimate.estimated_tokens,
            "estimated_rub": round(estimate.estimated_rub, 2),
            "estimated_wall_clock_min": round(estimate.estimated_wall_clock_min, 1),
            "actual_calls": actual_calls,
            "provider_attempts": provider_attempts,
            "cache_hits": cache.hits,
            "cache_misses": cache.misses,
        },
        "recall_modes": recall_modes,
        "errors": errors,
    }


def _per_class_snapshot_from_results(
    headline_results: list[TurnReplayResult],
    *,
    classes: tuple[str, ...] = _INVARIANT_CLASSES,
) -> dict[str, dict[str, int]]:
    """Lightweight per-class NEW-side verdict tally for the scorecard (no old
    baseline needed) — a quick view of how the new system did per class.

    ``classes`` restricts the tally (e.g. the no-memory safety subset).
    """
    snap: dict[str, dict[str, int]] = {
        cls: {"pass": 0, "fail": 0, "unknown": 0} for cls in classes
    }
    for res in headline_results:
        if res.invariants is None:
            for cls in classes:
                snap[cls]["unknown"] += 1
            continue
        verdicts = res.invariants.as_dict()
        for cls in classes:
            snap[cls][verdicts[cls]] += 1
    return snap


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------


def run_replay(
    *,
    export_path: str | Path,
    manifest_path: str | Path,
    lanes: tuple[str, ...] = (LANE_CURRENT, LANE_NO_MEMORY),
    limit: int | None = None,
    dry_run: bool = False,
    registry: RegistryBundle | None = None,
    embed_query_fn: Callable[[str], list[float]] | None = None,
    planner_call: Callable[..., Any] | None = None,
    caps: BudgetCaps | None = None,
    enforce_safety: bool = True,
) -> tuple[ScoringResult | None, dict[str, Any]]:
    """Run the offline replay end-to-end and return (verdict, scorecard).

    On ``dry_run=True`` returns ``(None, scorecard)`` where the scorecard
    holds only the budget estimate (no planner calls).

    Parameters mirror the CLI; ``registry`` / ``planner_call`` /
    ``embed_query_fn`` are injectable so the unit test can run fully offline.
    """
    if enforce_safety:
        assert_safe_to_run()

    caps = caps or BudgetCaps()

    records = load_export_records(export_path)
    manifest = load_manifest(manifest_path)
    pairing = pair_with_gold(records, manifest)

    # Only matched, gold-bearing cases are replayable; apply --limit.
    replayable = [c for c in pairing.cases if c.gold is not None]
    if limit is not None:
        replayable = replayable[:limit]

    # Template-only compose ⟹ 1 planner LLM call/turn/lane (no LLM-compose); FIX
    # 7a derives the calls-per-turn from the compose mode rather than a literal.
    estimate = estimate_budget(
        len(replayable),
        len(lanes),
        caps,
        calls_per_turn=calls_per_turn_for_compose_mode(COMPOSE_MODE_TEMPLATE_ONLY),
    )

    if dry_run:
        scorecard = {
            "dry_run": True,
            "estimate": {
                "turn_count": estimate.turn_count,
                "lane_count": estimate.lane_count,
                "estimated_calls": estimate.estimated_calls,
                "estimated_tokens": estimate.estimated_tokens,
                "estimated_rub": round(estimate.estimated_rub, 2),
                "within_caps": estimate.within_caps,
                "breaches": estimate.breaches,
            },
        }
        return None, scorecard

    # §14: refuse to start if the estimate exceeds ANY cap.
    if not estimate.within_caps:
        verdict = scoring.evaluate(
            ScoringInput(budget_aborted=True)
        )
        scorecard = {
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "budget_breaches": estimate.breaches,
        }
        return verdict, scorecard

    if registry is None:
        registry = load_production_registry()
    if planner_call is None:
        from sreda.runtime.planner.llm import call_planner

        def planner_call(prompt: str) -> Any:  # type: ignore[misc]
            return call_planner(prompt, timeout_seconds=caps.per_call_timeout_s)

    cache = RawCache()
    # FIX 7b (MAJOR #7b): accumulate REAL provider attempts (incl. retries)
    # across the whole run, separate from cache misses.
    attempt_counter = ProviderAttemptCounter()

    # turn_id_hash → gold case, for eligibility filtering + recon audit (FIX 1/5).
    gold_by_hash = {c.record.turn_id_hash: c for c in replayable}

    all_headline_results: list[TurnReplayResult] = []
    no_memory_results: list[TurnReplayResult] = []

    for case in replayable:
        for lane in lanes:
            res = replay_one_turn(
                case,
                lane=lane,
                registry=registry,
                embed_query_fn=embed_query_fn,
                planner_call=planner_call,
                cache=cache,
                attempt_counter=attempt_counter,
            )
            if lane == LANE_CURRENT:
                all_headline_results.append(res)
            elif lane == LANE_NO_MEMORY:
                no_memory_results.append(res)

    # --- FIX 1 (§5/§12): score the HEADLINE only over memory_eligible turns ---
    # The current-memory HEADLINE verdict (incl. valid_plan_rate numerator) is
    # computed ONLY over turns whose gold expectations.memory_eligible is True.
    # Ineligible turns are EXCLUDED and reported as attrition in the scorecard —
    # never silently dropped, never counted toward valid_plan/total.
    headline_results: list[TurnReplayResult] = []
    excluded_ineligible = 0
    for res in all_headline_results:
        case = gold_by_hash.get(res.turn_id_hash)
        eligible = bool(
            case is not None
            and case.gold is not None
            and case.gold.expectations.memory_eligible
        )
        if eligible:
            headline_results.append(res)
        else:
            excluded_ineligible += 1

    # valid_plan_count is computed ONLY over the eligible headline set (FIX 1),
    # so valid_plan_count ≤ total_eligible_count always holds.
    valid_plan_count = sum(1 for res in headline_results if res.valid_plan)
    total_eligible_count = len(headline_results)

    # --- FIX 5 (§7/§12): reconstruction-audit-failed gate --------------------
    # Stage-0 NO DECISION must fire on a REAL recon-audit failure, not just
    # incidental unknown coverage. A matched replayable headline case whose
    # ReconstructedCase.fidelity_ok is False is a recon-audit failure.
    reconstruction_audit_failed = any(
        not c.fidelity_ok for c in replayable
    )

    scoring_input = aggregate_scoring_input(
        headline_results,
        pairing=pairing,
        valid_plan_count=valid_plan_count,
        total_eligible_count=total_eligible_count,
        no_memory_results=no_memory_results,
        reconstruction_audit_failed=reconstruction_audit_failed,
    )

    verdict = scoring.evaluate(scoring_input)

    scorecard = build_scorecard(
        verdict=verdict,
        estimate=estimate,
        cache=cache,
        headline_results=headline_results,
        pairing=pairing,
        lanes=lanes,
        excluded_ineligible=excluded_ineligible,
        no_memory_results=no_memory_results,
        reconstruction_audit_failed=reconstruction_audit_failed,
        provider_attempts=attempt_counter.attempts,
    )
    return verdict, scorecard


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_lanes(value: str) -> tuple[str, ...]:
    lanes = tuple(x.strip() for x in value.split(",") if x.strip())
    bad = [lane for lane in lanes if lane not in _ALL_LANES]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown lane(s) {bad}; valid: {list(_ALL_LANES)}"
        )
    return lanes


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_replay",
        description="Offline-replay harness — Stage 1/2 orchestration (#87).",
    )
    parser.add_argument("--export", required=True, help="path to the export JSONL")
    parser.add_argument(
        "--manifest", required=True, help="path to replay_cases.jsonl (frozen gold)"
    )
    parser.add_argument(
        "--lanes", type=_parse_lanes, default=(LANE_CURRENT, LANE_NO_MEMORY),
        help="comma-separated lanes (current,no-memory)",
    )
    parser.add_argument("--limit", type=int, default=None, help="cap turns")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="compute the budget estimate only — no planner calls",
    )
    parser.add_argument(
        "--out", default=None, help="scorecard output path (JSON); default stdout"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        verdict, scorecard = run_replay(
            export_path=args.export,
            manifest_path=args.manifest,
            lanes=args.lanes,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except ReplaySafetyError as exc:
        print(f"SAFETY GATE: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(scorecard, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"scorecard written to {args.out}")
    else:
        print(payload)

    if verdict is None:
        return 0
    # Exit code mirrors the verdict for CI gating.
    return {"GO": 0, "NO-GO": 1, "NO DECISION": 3}.get(verdict.verdict, 1)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
