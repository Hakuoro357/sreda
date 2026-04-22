"""Live probe: does the configured LLM endpoint accept
``parallel_tool_calls=true`` and actually emit multiple tool_calls in
one assistant message when the prompt invites it?

Useful when evaluating a new provider or checking a MiMo upgrade.
Run from repo root:

    python scripts/dev/probe_parallel_tool_calls.py

Env overrides:
    SREDA_PROBE_BASE_URL  — override base_url (default: MiMo settings)
    SREDA_PROBE_API_KEY   — override api key
    SREDA_PROBE_MODEL     — override model name

Prints a short report: whether the server accepted the parameter,
how many tool_calls came back, which tools, and p95 wall-time.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from sreda.config.settings import get_settings


@tool
def get_shopping_list() -> str:
    """Return the user's current pending shopping list."""
    return "молоко; хлеб; яйца"


@tool
def get_menu_today() -> str:
    """Return the menu planned for today."""
    return "Завтрак: омлет. Обед: борщ. Ужин: плов."


def main() -> int:
    settings = get_settings()
    base_url = os.environ.get("SREDA_PROBE_BASE_URL") or settings.mimo_base_url
    api_key = os.environ.get("SREDA_PROBE_API_KEY") or settings.resolve_mimo_api_key()
    model = os.environ.get("SREDA_PROBE_MODEL") or settings.mimo_chat_model

    if not api_key:
        print("[!] no api key (set SREDA_PROBE_API_KEY or configure MiMo)")
        return 2

    print(f"[i] base_url = {base_url}")
    print(f"[i] model    = {model}")
    print()

    question = (
        "Что у меня сейчас в списке покупок и что в меню на сегодня? "
        "Вызови оба нужных tool'а, мне нужны обе справки в одном ответе."
    )

    # Probe A: with parallel_tool_calls=True
    print("[A] parallel_tool_calls = True")
    try:
        llm_on = ChatOpenAI(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=0.2,
            timeout=60,
            model_kwargs={"parallel_tool_calls": True},
        ).bind_tools([get_shopping_list, get_menu_today])
        t0 = time.monotonic()
        resp_on = llm_on.invoke(question)
        dt_on = time.monotonic() - t0
        calls_on = [tc.get("name") for tc in (resp_on.tool_calls or [])]
        print(f"    ok  — tool_calls={calls_on}  wall={dt_on:.2f}s")
    except Exception as e:  # noqa: BLE001 — probe, we want raw err
        print(f"    REJECTED — {type(e).__name__}: {e}")
        calls_on = None

    print()

    # Probe B: default (baseline)
    print("[B] parallel_tool_calls = default (unset)")
    try:
        llm_off = ChatOpenAI(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=0.2,
            timeout=60,
        ).bind_tools([get_shopping_list, get_menu_today])
        t0 = time.monotonic()
        resp_off = llm_off.invoke(question)
        dt_off = time.monotonic() - t0
        calls_off = [tc.get("name") for tc in (resp_off.tool_calls or [])]
        print(f"    ok  — tool_calls={calls_off}  wall={dt_off:.2f}s")
    except Exception as e:  # noqa: BLE001
        print(f"    failed — {type(e).__name__}: {e}")
        calls_off = None

    print()
    print("---- verdict ----")
    if calls_on is None:
        print("server REJECTED parallel_tool_calls parameter")
        return 1
    if len(calls_on) >= 2:
        print("server ACCEPTS parameter AND emitted parallel tool_calls.")
        print(f"   -> safe to enable, expected saving ~1 LLM round-trip per multi-read turn.")
    elif calls_off is not None and len(calls_off) >= 2:
        print("server emits parallel tool_calls by default — no need to set the flag.")
    else:
        print("server accepts the flag but still serialises calls.")
        print("   -> no speed win from enabling; model chose to do one call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
