"""Benchmark harness for chat LLM providers.

Runs a set of canonical agent-turn scenarios against each configured
provider, measures wall-time per iteration, counts tool_calls, and
prints a comparison table. Purpose: decide which provider to A/B in
production for Сpeda's housewife skill.

Not unit-tested: this is a research-time script, meant to be re-run
manually whenever we re-benchmark.

Run from repo root:

    python scripts/dev/benchmark_llm_providers.py

Reads provider secrets from:
    .secrets/mimo_api_key.txt
    .secrets/cerebras_chat_token.txt

Each scenario is a (system_prompt + user_text + tools) setup. The
runner drives a simple tool-loop (max 4 iterations) to completion
and records end-to-end wall time and token counts. Scenarios are
chosen to reflect real production turn shapes we've observed:

    A) Single read  — 'что у меня в списке покупок'
    B) Multi read   — 'что в списке и что в меню на сегодня'
    C) Batch write  — 'сохрани 5 рецептов: борщ, омлет, плов, котлеты, блины'
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from sreda.runtime.handlers import build_system_prompt


# ---------------------------------------------------------------------------
# Provider matrix
# ---------------------------------------------------------------------------


def _read_secret(rel_path: str) -> str | None:
    p = Path(rel_path)
    if not p.exists():
        return None
    val = p.read_text(encoding="utf-8").strip()
    return val or None


def _openrouter_key() -> str | None:
    # OpenRouter token file is markdown with the bare key on line 1.
    p = Path(".secrets/openrouter-token.md")
    if not p.exists():
        return None
    first_line = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    return first_line or None


PROVIDERS = [
    # Baseline — current production provider. No price tracking.
    {
        "label": "MiMo-V2-Pro",
        "base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
        "api_key": _read_secret(".secrets/mimo_api_key.txt"),
        "model": "mimo-v2-pro",
        "price_in": 0.0, "price_out": 0.0,
    },
    # OpenRouter paid candidates (prices per 1M tokens, verified
    # against /api/v1/models on 2026-04-22).
    {
        "label": "OR/gemma-4-26b-moe",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "google/gemma-4-26b-a4b-it",
        "price_in": 0.07, "price_out": 0.35,
    },
    {
        "label": "OR/gemma-4-31b",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "google/gemma-4-31b-it",
        "price_in": 0.13, "price_out": 0.38,
    },
    {
        "label": "OR/grok-4.1-fast",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "x-ai/grok-4.1-fast",
        "price_in": 0.20, "price_out": 0.50,
    },
    {
        "label": "OR/minimax-m2.7",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "minimax/minimax-m2.7",
        "price_in": 0.30, "price_out": 1.20,
    },
    {
        "label": "OR/qwen3.6-plus",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "qwen/qwen3.6-plus",
        "price_in": 0.33, "price_out": 1.95,
    },
    {
        "label": "OR/kimi-k2.5",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "moonshotai/kimi-k2.5",
        "price_in": 0.44, "price_out": 2.00,
    },
]


# ---------------------------------------------------------------------------
# Stub tools — realistic shapes but no DB, so every provider sees the
# same behaviour. Tool RESULTS hardcoded so the LLM has something to
# consume in the second iteration.
# ---------------------------------------------------------------------------


@tool
def list_shopping() -> str:
    """Return the user's current pending shopping list (title, id, quantity)."""
    return json.dumps(
        [
            {"id": "si_1", "title": "молоко", "quantity_text": "1 л"},
            {"id": "si_2", "title": "хлеб", "quantity_text": None},
            {"id": "si_3", "title": "яйца", "quantity_text": "10 шт"},
        ],
        ensure_ascii=False,
    )


@tool
def list_menu() -> str:
    """Return the planned menu for today (breakfast, lunch, dinner)."""
    return json.dumps(
        {
            "date": "2026-04-22",
            "breakfast": "омлет",
            "lunch": "борщ",
            "dinner": "плов с курицей",
        },
        ensure_ascii=False,
    )


@tool
def search_recipes(query: str = "") -> str:
    """Return all saved recipes in the user's book, optionally filtered by query."""
    return json.dumps(
        [
            {"id": "rec_1", "title": "Борщ"},
            {"id": "rec_2", "title": "Окрошка"},
        ],
        ensure_ascii=False,
    )


@tool
def save_recipes_batch(recipes: list[dict]) -> str:
    """Save a batch of recipes at once. Each recipe must have {title, ingredients, source}."""
    return f"ok:created={len(recipes)}:skipped=0"


TOOLS = [list_shopping, list_menu, search_recipes, save_recipes_batch]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


SCENARIOS = [
    {
        "name": "A.single_read",
        "user_text": "что у меня сейчас в списке покупок?",
        "expected_tools": ["list_shopping"],
    },
    {
        "name": "B.multi_read",
        "user_text": "что у меня в списке покупок и какое меню на сегодня?",
        "expected_tools": ["list_shopping", "list_menu"],
    },
    {
        "name": "C.batch_write",
        "user_text": (
            "сохрани пять рецептов с ингредиентами: "
            "борщ, омлет, плов с курицей, котлеты, блины с творогом"
        ),
        "expected_tools": ["save_recipes_batch"],
    },
]


SYSTEM_PROMPT = build_system_prompt("housewife_assistant")
MAX_ITERS = 4


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_one_turn(provider, scenario):
    if not provider["api_key"]:
        return {"error": "no api_key", "provider": provider["label"]}
    llm = ChatOpenAI(
        base_url=provider["base_url"],
        api_key=provider["api_key"],
        model=provider["model"],
        temperature=0.2,
        timeout=60,
    ).bind_tools(TOOLS)

    tools_by_name = {t.name: t for t in TOOLS}
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=scenario["user_text"]),
    ]
    iters = 0
    observed_tools: list[str] = []
    total_in = 0
    total_out = 0
    final_text = ""

    t_start = time.monotonic()
    try:
        for i in range(MAX_ITERS):
            iters += 1
            ai = llm.invoke(messages)
            messages.append(ai)
            usage = getattr(ai, "usage_metadata", None) or {}
            total_in += int(usage.get("input_tokens") or 0)
            total_out += int(usage.get("output_tokens") or 0)
            calls = getattr(ai, "tool_calls", None) or []
            observed_tools.extend(tc.get("name", "?") for tc in calls)
            if not calls:
                final_text = str(ai.content or "")[:200]
                break
            for tc in calls:
                tname = tc.get("name")
                targs = tc.get("args") or {}
                tc_id = tc.get("id", "")
                tool = tools_by_name.get(tname)
                if tool is None:
                    result = f"error:unknown_tool:{tname}"
                else:
                    try:
                        result = tool.invoke(targs)
                    except Exception as e:  # noqa: BLE001
                        result = f"error:{type(e).__name__}:{e}"
                messages.append(ToolMessage(content=str(result), tool_call_id=tc_id))
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "provider": provider["label"],
            "scenario": scenario["name"],
        }
    wall = time.monotonic() - t_start

    return {
        "provider": provider["label"],
        "scenario": scenario["name"],
        "wall_s": round(wall, 2),
        "iters": iters,
        "tools": observed_tools,
        "in_tok": total_in,
        "out_tok": total_out,
        "final_preview": final_text,
    }


def main(runs_per_pair: int = 2) -> int:
    print(f"System-prompt size: {len(SYSTEM_PROMPT)} chars")
    print(f"Providers: {len(PROVIDERS)}, scenarios: {len(SCENARIOS)}, runs each: {runs_per_pair}")
    print()

    header = (
        f"{'provider':<24} {'scenario':<16} {'wall (s)':>10} {'iters':>6} "
        f"{'in_tok':>7} {'out_tok':>8} {'cost $':>10} {'tools':<40}"
    )
    print(header)
    print("-" * len(header))
    total_cost_by_provider: dict[str, float] = {}

    agg: dict[tuple[str, str], list[float]] = {}

    for provider in PROVIDERS:
        # Cerebras qwen-3-235b preview is rate-limited to 5 RPM.
        # Pause between runs so we don't blow the quota and miss all data.
        slow_provider = "qwen-3-235b" in provider["model"]
        for scenario in SCENARIOS:
            walls: list[float] = []
            last = None
            for _ in range(runs_per_pair):
                if slow_provider:
                    time.sleep(15)
                last = _run_one_turn(provider, scenario)
                if last.get("error"):
                    print(f"{provider['label']:<24} {scenario['name']:<16}   ERR   {last['error'][:80]}")
                    break
                walls.append(last["wall_s"])
            if walls and last and "error" not in last:
                median = round(statistics.median(walls), 2)
                agg[(provider["label"], scenario["name"])] = walls
                tools_str = ",".join(last.get("tools", []))[:40]
                # Cost for the LAST run shown (median chosen visually;
                # tokens may drift across runs but order of magnitude
                # identical). Summed per-provider below for a true total.
                cost_one = (
                    last["in_tok"] * provider["price_in"] / 1_000_000
                    + last["out_tok"] * provider["price_out"] / 1_000_000
                )
                total_cost_by_provider[provider["label"]] = (
                    total_cost_by_provider.get(provider["label"], 0.0)
                    + cost_one * len(walls)
                )
                print(
                    f"{provider['label']:<24} {scenario['name']:<16} "
                    f"{median:>10} {last['iters']:>6} {last['in_tok']:>7} "
                    f"{last['out_tok']:>8} {cost_one:>10.5f} {tools_str:<40}"
                )
                fp = last.get("final_preview") or ""
                if fp:
                    print(f"    reply: {fp[:160]!r}")

    print()
    print("---- total spend per provider (all scenarios × all runs) ----")
    grand = 0.0
    for label, cost in sorted(total_cost_by_provider.items(), key=lambda kv: kv[1]):
        grand += cost
        print(f"  {label:<24} ${cost:.5f}")
    print(f"  {'GRAND TOTAL':<24} ${grand:.5f}")
    print()
    print("---- median wall per (provider, scenario) ----")
    header2 = f"{'provider':<24}" + "".join(f" {s['name']:>16}" for s in SCENARIOS)
    print(header2)
    for provider in PROVIDERS:
        row = f"{provider['label']:<24}"
        for scenario in SCENARIOS:
            walls = agg.get((provider["label"], scenario["name"]))
            if walls:
                row += f" {statistics.median(walls):>16.2f}"
            else:
                row += f" {'ERR':>16}"
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
