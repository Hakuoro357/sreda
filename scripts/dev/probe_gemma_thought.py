"""Isolate and defeat Gemma-4-26b-moe's 'thought\\n' prefix.

Gemma on OpenRouter sometimes prepends a literal "thought\\n" marker
to its reply. We need to know:
  1. Is it the model, or an OpenRouter rendering artifact?
  2. Can a system-prompt rule suppress it?
  3. Can response_format / reasoning off-switch kill it?

Three probe rounds, 3 samples each. Prints raw content bytes so we
see exactly what lands — no silent stripping.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI


def _key() -> str:
    p = Path(".secrets/openrouter-token.md")
    return p.read_text(encoding="utf-8").strip().splitlines()[0].strip()


MODEL = "google/gemma-4-26b-a4b-it"
BASE = "https://openrouter.ai/api/v1"


def ask(system: str, user: str, *, extra_kwargs: dict | None = None) -> str:
    llm = ChatOpenAI(
        base_url=BASE, api_key=_key(), model=MODEL,
        temperature=0.2, timeout=30,
        model_kwargs=(extra_kwargs or {}),
    )
    t0 = time.monotonic()
    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    dt = time.monotonic() - t0
    content = str(resp.content or "")
    return f"[{dt:.2f}s] {content!r}"


def main() -> int:
    user = "скажи коротко что ты умеешь, одним предложением"

    print("=== Round 1: BASELINE (empty system prompt) ===")
    print(ask("", user))
    print()

    print("=== Round 2: minimal persona ===")
    print(ask("Ты ассистент Среда. Отвечай на русском.", user))
    print()

    print("=== Round 3: explicit anti-thought rule ===")
    print(ask(
        "Ты ассистент Среда. Отвечай на русском. НЕ выводи внутренние "
        "рассуждения, thinking-traces, meta-префиксы типа 'thought:' "
        "или 'reasoning:'. Сразу финальный ответ.",
        user,
    ))
    print()

    print("=== Round 4: current full prompt (housewife) ===")
    from sreda.runtime.handlers import build_system_prompt
    print(ask(build_system_prompt("housewife_assistant"), user))
    print()

    print("=== Round 5: current prompt + anti-thought prefix ===")
    from sreda.runtime.handlers import build_system_prompt as b
    augmented = (
        "НЕ выводи thinking-traces / meta-префиксы ('thought:', "
        "'reasoning:', etc). Сразу финальный ответ пользователю.\n\n"
        + b("housewife_assistant")
    )
    print(ask(augmented, user))
    print()

    print("=== Round 6: response_format=text (no json mode) ===")
    print(ask(
        "Ты ассистент. Отвечай на русском коротко.",
        user,
        extra_kwargs={"response_format": {"type": "text"}},
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
