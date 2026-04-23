"""Focused bench: MiMo-V2-Pro vs MiMo-V2.5-Pro.

Same 3 canonical housewife scenarios the main benchmark uses, but
only two providers so the matrix fits on one screen and we don't
burn quota on the rest. Both variants share the same API key and
base_url; only ``model`` differs.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from sreda.runtime.handlers import build_system_prompt


def _read_secret(rel_path: str) -> str | None:
    p = Path(rel_path)
    if not p.exists():
        return None
    val = p.read_text(encoding="utf-8").strip()
    return val or None


MIMO_KEY = _read_secret(".secrets/mimo_api_key.txt")
MIMO_BASE_URL = "https://token-plan-sgp.xiaomimimo.com/v1"

PROVIDERS = [
    {"label": "MiMo-V2-Pro",        "model": "mimo-v2-pro"},
    {"label": "MiMo-V2.5-Pro",      "model": "mimo-v2.5-pro"},
    {"label": "MiMo-V2.5-light",    "model": "mimo-v2.5"},
]


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
        {"date": "2026-04-22", "breakfast": "омлет",
         "lunch": "борщ", "dinner": "плов с курицей"},
        ensure_ascii=False,
    )


@tool
def search_recipes(query: str = "") -> str:
    """Return all saved recipes in the user's book."""
    return json.dumps(
        [{"id": "rec_1", "title": "Борщ"}, {"id": "rec_2", "title": "Окрошка"}],
        ensure_ascii=False,
    )


@tool
def save_recipes_batch(recipes: list[dict]) -> str:
    """Save a batch of recipes."""
    return f"ok:created={len(recipes)}:skipped=0"


TOOLS = [list_shopping, list_menu, search_recipes, save_recipes_batch]

SCENARIOS = [
    {
        "name": "A.single_read",
        "user_text": "что у меня сейчас в списке покупок?",
    },
    {
        "name": "B.multi_read",
        "user_text": "что у меня в списке покупок и какое меню на сегодня?",
    },
    {
        "name": "C.batch_write",
        "user_text": (
            "сохрани пять рецептов с ингредиентами: "
            "борщ, омлет, плов с курицей, котлеты, блины с творогом"
        ),
    },
]

SYSTEM_PROMPT = build_system_prompt("housewife_assistant")
MAX_ITERS = 4


def _run_one_turn(model: str, scenario: dict) -> dict:
    if not MIMO_KEY:
        return {"error": "no mimo key"}
    llm = ChatOpenAI(
        base_url=MIMO_BASE_URL,
        api_key=MIMO_KEY,
        model=model,
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
    t0 = time.monotonic()
    try:
        for _ in range(MAX_ITERS):
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
                result = tools_by_name[tc["name"]].invoke(tc.get("args") or {})
                messages.append(
                    ToolMessage(content=str(result), tool_call_id=tc.get("id", ""))
                )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "wall_s": round(time.monotonic() - t0, 2),
        "iters": iters,
        "tools": observed_tools,
        "in_tok": total_in,
        "out_tok": total_out,
        "final": final_text,
    }


def main(runs: int = 2) -> int:
    print(f"MiMo V2 vs V2.5 bench — {runs} runs per (model,scenario)\n")
    results: dict[tuple[str, str], list[dict]] = {}
    for provider in PROVIDERS:
        for scenario in SCENARIOS:
            key = (provider["label"], scenario["name"])
            results[key] = []
            for i in range(runs):
                res = _run_one_turn(provider["model"], scenario)
                results[key].append(res)
                if res.get("error"):
                    print(f"  {provider['label']:<16} {scenario['name']:<16} ERR {res['error'][:80]}")
                    break
                print(
                    f"  {provider['label']:<16} {scenario['name']:<16} "
                    f"run{i} wall={res['wall_s']}s iters={res['iters']} "
                    f"tok_in={res['in_tok']} tok_out={res['out_tok']} "
                    f"tools={res['tools']}"
                )
                if i == 0 and res.get("final"):
                    print(f"    reply: {res['final'][:120]!r}")

    print("\n---- median wall (s) per (model, scenario) ----")
    header = f"{'model':<16} " + " ".join(f"{s['name']:>16}" for s in SCENARIOS)
    print(header)
    for provider in PROVIDERS:
        row = f"{provider['label']:<16}"
        for scenario in SCENARIOS:
            key = (provider["label"], scenario["name"])
            walls = [r["wall_s"] for r in results.get(key, []) if "wall_s" in r]
            if walls:
                row += f" {statistics.median(walls):>16.2f}"
            else:
                row += f" {'ERR':>16}"
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
