"""Quick Russian-quality sanity check for Ling-2.6-flash vs MiMo.

Prints each final assistant reply for the 3 canonical scenarios so
a human can spot-check readability, tone, and correctness of the
generated Russian text before committing to Ling as the production
provider.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from sreda.runtime.handlers import build_system_prompt


def _read_first_line(path: str) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip().splitlines()[0].strip() or None


PROVIDERS = [
    ("MiMo-V2-Pro", "https://token-plan-sgp.xiaomimimo.com/v1",
     _read_first_line(".secrets/mimo_api_key.txt"), "mimo-v2-pro"),
    ("OR/ling-2.6-flash", "https://openrouter.ai/api/v1",
     _read_first_line(".secrets/openrouter-token.md"), "inclusionai/ling-2.6-flash:free"),
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
    ("A. Simple read", "что у меня сейчас в списке покупок?"),
    ("B. Multi read", "что у меня в списке покупок и какое меню на сегодня?"),
    ("C. Batch write", "сохрани пять рецептов с ингредиентами: борщ, омлет, плов с курицей, котлеты, блины с творогом"),
    ("D. Conversation", "скажи коротко — что такое выходной день по-твоему? в одном предложении."),
]


def run(provider, scenario_name, question):
    label, base_url, api_key, model = provider
    if not api_key:
        print(f"  [{label}] no api_key — skip")
        return
    llm = ChatOpenAI(
        base_url=base_url, api_key=api_key, model=model,
        temperature=0.2, timeout=60,
    ).bind_tools(TOOLS)
    tools_by_name = {t.name: t for t in TOOLS}
    messages = [
        SystemMessage(content=build_system_prompt("housewife_assistant")),
        HumanMessage(content=question),
    ]
    t0 = time.monotonic()
    try:
        for _ in range(5):
            ai = llm.invoke(messages)
            messages.append(ai)
            calls = getattr(ai, "tool_calls", None) or []
            if not calls:
                dt = time.monotonic() - t0
                print(f"  [{label}] {dt:.2f}s")
                print(f"    reply: {(ai.content or '').strip()[:400]}")
                return
            for tc in calls:
                res = tools_by_name[tc["name"]].invoke(tc.get("args") or {})
                messages.append(ToolMessage(content=str(res), tool_call_id=tc.get("id", "")))
    except Exception as e:  # noqa: BLE001
        print(f"  [{label}] ERR {type(e).__name__}: {e}")


def main() -> int:
    for name, q in SCENARIOS:
        print(f"=== {name} ===")
        print(f"user: {q}")
        for provider in PROVIDERS:
            run(provider, name, q)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
