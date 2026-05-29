"""Live eval CLI for the planner — Sub-A12 Phase B.7.

Runs N synthetic prompts through the real orchestrator + real LLM and
reports valid-plan rate, retry rate, latency percentiles. Used as a
pre-Phase-E gate: "does the production planner prompt + mimo-v2.5
actually produce valid plans?".

CLI usage::

    python -m scripts.eval_planner_live \\
        --limit 5 \\
        --require-valid-rate 0.90 \\
        --require-p95-latency-ms 15000 \\
        --tenant-id eval_local

The script:
1. Loads synthetic scenarios from few_shot_examples.all_examples()
2. For each, builds a PlannerContext and calls orchestrator.run() with
   real ``services.llm.get_chat_llm`` — NO mock.
3. Counts valid / invalid / retry / latency.
4. Prints a markdown summary.
5. Exits 0 if thresholds met, non-zero otherwise.

This is NOT a CI test. It's a manual smoke for Phase E rollout. Real
LLM calls cost money and time; run intentionally.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sreda.config.settings import get_settings
from sreda.runtime.planner.few_shot_examples import all_examples
from sreda.runtime.planner.orchestrator import (
    PlannerContext,
    PlannerResult,
    run as orchestrator_run,
)
from sreda.runtime.planner.prompt_builder import NowMoment, ProfileSnapshot
from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS


logger = logging.getLogger("eval_planner_live")


# ---------------------------------------------------------------------------
# Per-scenario result + aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioOutcome:
    label: str
    user_message: str
    success: bool
    final_attempt_no: int
    latency_ms: int
    error_summary: str | None


@dataclass(frozen=True)
class EvalReport:
    """Aggregated outcomes + threshold compliance flags."""

    outcomes: tuple[ScenarioOutcome, ...]
    valid_rate: float
    retry_rate: float
    p50_latency_ms: int
    p95_latency_ms: int

    @property
    def thresholds_met(self) -> bool:
        return self.valid_rate >= self._req_valid_rate and self.p95_latency_ms <= self._req_p95_ms

    # Threshold inputs stored on the dataclass for downstream-friendly
    # access; set via with_thresholds().
    _req_valid_rate: float = 0.0
    _req_p95_ms: int = 0

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append("# Planner live-eval report")
        lines.append("")
        lines.append(f"- generated: {datetime.utcnow().isoformat()}Z")
        lines.append(f"- scenarios: {len(self.outcomes)}")
        lines.append(f"- valid: {sum(1 for o in self.outcomes if o.success)} / {len(self.outcomes)}")
        lines.append(f"- valid rate: {self.valid_rate:.2%}")
        lines.append(f"- retry rate: {self.retry_rate:.2%}")
        lines.append(f"- p50 latency: {self.p50_latency_ms} ms")
        lines.append(f"- p95 latency: {self.p95_latency_ms} ms")
        lines.append("")
        lines.append("| # | user_message | success | attempt | latency_ms | error |")
        lines.append("|---|---|---|---|---|---|")
        for i, o in enumerate(self.outcomes, start=1):
            err = o.error_summary or ""
            msg = o.user_message[:60].replace("|", "/")
            lines.append(
                f"| {i} | {msg} | {'OK' if o.success else 'FAIL'} | "
                f"{o.final_attempt_no} | {o.latency_ms} | {err} |"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Aggregation pipeline
# ---------------------------------------------------------------------------


def aggregate(
    outcomes: list[ScenarioOutcome],
    *,
    require_valid_rate: float,
    require_p95_latency_ms: int,
) -> EvalReport:
    """Build EvalReport with percentiles + threshold flags."""
    if not outcomes:
        return EvalReport(
            outcomes=(),
            valid_rate=0.0,
            retry_rate=0.0,
            p50_latency_ms=0,
            p95_latency_ms=0,
            _req_valid_rate=require_valid_rate,
            _req_p95_ms=require_p95_latency_ms,
        )

    total = len(outcomes)
    valids = sum(1 for o in outcomes if o.success)
    retries = sum(1 for o in outcomes if o.final_attempt_no > 1)
    latencies = sorted(o.latency_ms for o in outcomes)
    p50 = latencies[len(latencies) // 2]
    # p95: ceil(0.95 * n) - 1 — for n=5 picks index 4 (the worst);
    # for n=20 picks index 18 (second-worst), matching standard
    # nearest-rank percentile convention. Capped at last index.
    p95_idx = max(0, min(len(latencies) - 1, math.ceil(0.95 * len(latencies)) - 1))
    p95 = latencies[p95_idx]
    return EvalReport(
        outcomes=tuple(outcomes),
        valid_rate=valids / total,
        retry_rate=retries / total,
        p50_latency_ms=p50,
        p95_latency_ms=p95,
        _req_valid_rate=require_valid_rate,
        _req_p95_ms=require_p95_latency_ms,
    )


# ---------------------------------------------------------------------------
# Real orchestrator run + DB fake-runs setup
# ---------------------------------------------------------------------------


_MSK = ZoneInfo("Europe/Moscow")


def _make_ctx(user_message: str, *, tenant_id: str, run_id: str) -> PlannerContext:
    return PlannerContext(
        tenant_id=tenant_id,
        run_id=run_id,
        feature_key="housewife_assistant",
        user_message=user_message,
        voice_meta=None,
        # Codex B.7 R1 MEDIUM fix: NowMoment renders Europe/Moscow, so
        # passing utcnow() shifted the prompt clock by ~3 hours. Use
        # MSK-local time to match production.
        now=NowMoment(datetime.now(_MSK).replace(tzinfo=None)),
        profile=ProfileSnapshot(name="Eval"),
        memories=(),
        active_turn=None,
        closed_turns=(),
        available_tools=tuple(MIGRATED_TOOL_SPECS),
        composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
        # Sub-A12 Phase D.2-enable R2 — pass the full registry as the
        # PROPOSED key set; the orchestrator gates it down to
        # settings.composer_llm_enabled_keys (default empty → none), so
        # the kill-switch is enforced centrally, not here.
        composer_llm_prompt_keys=tuple(LLM_PROMPT_REGISTRY.prompt_keys()),
        composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
        tool_registry_version="live-eval",
        few_shot_block="",
        planner_provider=None,  # use settings default
    )


@contextmanager
def _real_session_factory_context() -> Iterator:
    """Yield None (ephemeral mode).

    Codex B.7 R1 MAJOR fix: live eval can't write to ``planner_executions``
    because ``run_id`` is FK to ``agent_runs.id``, and synthetic eval IDs
    don't have matching agent_runs rows. Pre-creating them adds enough
    workspace/thread/tenant FK plumbing that the eval script stops being
    "manual scratch tool". The cleaner contract: orchestrator supports
    ``session_factory=None`` (ephemeral mode, no DB writes), and the
    eval script uses that. Audit moves to the markdown report on disk.
    """
    yield None


async def _run_one(
    label: str, user_message: str, *, tenant_id: str, run_id: str,
    session_factory,
) -> ScenarioOutcome:
    """Run a single scenario through the real orchestrator."""
    ctx = _make_ctx(user_message, tenant_id=tenant_id, run_id=run_id)
    started = datetime.utcnow()
    try:
        result: PlannerResult = await orchestrator_run(
            ctx, session_factory=session_factory,
        )
    except Exception as exc:  # noqa: BLE001 — eval shouldn't crash on per-scenario errors
        logger.exception("eval scenario %s crashed", label)
        return ScenarioOutcome(
            label=label,
            user_message=user_message,
            success=False,
            final_attempt_no=0,
            latency_ms=int((datetime.utcnow() - started).total_seconds() * 1000),
            error_summary=f"crashed:{type(exc).__name__}",
        )
    elapsed_ms = int((datetime.utcnow() - started).total_seconds() * 1000)
    return ScenarioOutcome(
        label=label,
        user_message=user_message,
        success=result.success,
        final_attempt_no=result.final_attempt_no,
        latency_ms=elapsed_ms,
        error_summary=result.error_summary,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="eval_planner_live",
        description="Live planner eval — runs N synthetic prompts against real LLM.",
    )
    p.add_argument(
        "--limit", type=int, default=5,
        help="How many synthetic scenarios to run (from few_shot_examples).",
    )
    p.add_argument(
        "--require-valid-rate", type=float, default=0.90,
        help="Threshold: valid plan rate ≥ this value or exit non-zero.",
    )
    p.add_argument(
        "--require-p95-latency-ms", type=int, default=15000,
        help="Threshold: p95 latency ≤ this value or exit non-zero.",
    )
    p.add_argument(
        "--tenant-id", default="eval_local",
        help="tenant_id for the planner_executions rows.",
    )
    p.add_argument(
        "--output", type=Path, default=None,
        help="Optional path to write the markdown report; default = stdout.",
    )
    return p.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    logger.info(
        "eval: provider=%s prompt_version=%d tenant=%s limit=%d",
        settings.planner_provider,
        settings.planner_prompt_version,
        args.tenant_id,
        args.limit,
    )

    examples = all_examples()[: args.limit]
    outcomes: list[ScenarioOutcome] = []
    with _real_session_factory_context() as factory:
        for i, ex in enumerate(examples, start=1):
            run_id = f"eval_run_{i:03d}"
            outcome = await _run_one(
                label=f"synth:{i:02d}",
                user_message=ex.user_message,
                tenant_id=args.tenant_id,
                run_id=run_id,
                session_factory=factory,
            )
            outcomes.append(outcome)
            logger.info(
                "scenario %d: %s (attempt=%d, %dms)",
                i,
                "OK" if outcome.success else f"FAIL ({outcome.error_summary})",
                outcome.final_attempt_no,
                outcome.latency_ms,
            )

    report = aggregate(
        outcomes,
        require_valid_rate=args.require_valid_rate,
        require_p95_latency_ms=args.require_p95_latency_ms,
    )
    md = report.to_markdown()
    if args.output is not None:
        args.output.write_text(md, encoding="utf-8")
        logger.info("report written to %s", args.output)
    else:
        print(md)

    return 0 if report.thresholds_met else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    sys.exit(main())
