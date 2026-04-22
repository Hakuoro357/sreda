"""Reproduce the 'thought\\n' prefix by binding tools."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


def _key() -> str:
    return Path(".secrets/openrouter-token.md").read_text(encoding="utf-8").strip().splitlines()[0].strip()


@tool
def list_shopping() -> str:
    """Return the user's pending shopping list."""
    return json.dumps([
        {"id": "si_1", "title": "молоко", "quantity_text": "1 л"},
        {"id": "si_2", "title": "хлеб"},
        {"id": "si_3", "title": "яйца", "quantity_text": "10 шт"},
    ], ensure_ascii=False)


def run(system: str, user_text: str) -> str:
    llm = ChatOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=_key(),
        model="google/gemma-4-26b-a4b-it",
        temperature=0.2, timeout=30,
    ).bind_tools([list_shopping])
    messages: list = [SystemMessage(content=system), HumanMessage(content=user_text)]
    t0 = time.monotonic()
    # iter 1: expect tool call
    ai = llm.invoke(messages)
    messages.append(ai)
    calls = getattr(ai, "tool_calls", None) or []
    if calls:
        for tc in calls:
            messages.append(ToolMessage(
                content=list_shopping.invoke(tc.get("args") or {}),
                tool_call_id=tc.get("id", ""),
            ))
        # iter 2: expect text
        ai = llm.invoke(messages)
    dt = time.monotonic() - t0
    return f"[{dt:.2f}s] content={str(ai.content or '')!r}"


def main() -> int:
    question = "что у меня в списке покупок?"

    print("=== Round 1: short persona ===")
    print(run("Ты ассистент Среда. Отвечай на русском.", question))
    print()

    print("=== Round 2: anti-thought in short persona ===")
    print(run(
        "Ты ассистент Среда. Отвечай на русском. НЕ начинай ответ с "
        "'thought', 'thinking', 'reasoning' и подобных мета-префиксов. "
        "Только финальный текст для пользователя.",
        question,
    ))
    print()

    print("=== Round 3: full housewife prompt (reproduces bench artifact?) ===")
    from sreda.runtime.handlers import build_system_prompt
    print(run(build_system_prompt("housewife_assistant"), question))
    print()

    print("=== Round 4: full prompt + anti-thought nudge ===")
    from sreda.runtime.handlers import build_system_prompt as b
    system = (
        b("housewife_assistant")
        + "\n\n"
        + "ФОРМАТ ОТВЕТА: Не начинай ответ с 'thought', 'thinking', "
        "'reasoning' или подобных служебных слов. Никаких thinking-traces. "
        "Сразу финальный текст для пользователя."
    )
    print(run(system, question))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
