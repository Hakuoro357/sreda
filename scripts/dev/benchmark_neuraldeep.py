"""Ad-hoc benchmark of api.neuraldeep.ru against our standard scenarios.

Reuses the same harness shape as ``benchmark_llm_providers.py`` (3 chat
scenarios, tool-loop with stub tools, housewife system prompt) and
``probe_groq_whisper.py`` (5 Russian TTS samples → STT round-trip).
Tests:

  * qwen3.6-35b-a3b (chat)
  * gpt-oss-120b   (chat)
  * whisper-1      (STT)

Usage:

    python scripts/dev/benchmark_neuraldeep.py

The provider is in RU, so latency comparisons against MiMo (Singapore)
and Groq (US) are fair only for cold-path. Two runs per scenario for a
median, plus 2× warm-up call per chat model so we don't measure TLS
handshake.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from sreda.runtime.handlers import build_system_prompt


BASE_URL = "https://api.neuraldeep.ru/v1"
API_KEY = os.environ.get("NEURALDEEP_API_KEY") or "sk-qzSw9U6wW2MXU1e2DuuGyg"

CHAT_MODELS = [
    {"label": "ND/qwen3.6-35b-a3b", "model": "qwen3.6-35b-a3b"},
    {"label": "ND/gpt-oss-120b",    "model": "gpt-oss-120b"},
]

WHISPER_MODEL = "whisper-1"


# ---------------------------------------------------------------------------
# Tools (identical shape to benchmark_llm_providers.py)
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
RUNS_PER_PAIR = 1  # api.neuraldeep.ru rate-limit = 20 RPM на ключ
INTER_CALL_SLEEP = 3.5  # секунд между LLM-вызовами, чтобы не упереться в RPM


# ---------------------------------------------------------------------------
# Chat benchmark
# ---------------------------------------------------------------------------


def _run_one_turn(provider, scenario):
    llm = ChatOpenAI(
        base_url=BASE_URL,
        api_key=API_KEY,
        model=provider["model"],
        temperature=0.2,
        timeout=120,
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
                tname = tc.get("name")
                targs = tc.get("args") or {}
                tc_id = tc.get("id", "")
                tool_obj = tools_by_name.get(tname)
                if tool_obj is None:
                    result = f"error:unknown_tool:{tname}"
                else:
                    try:
                        result = tool_obj.invoke(targs)
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


def run_chat_benchmark() -> dict:
    print(f"System-prompt size: {len(SYSTEM_PROMPT)} chars")
    print(f"Endpoint: {BASE_URL}")
    print(f"Chat models: {len(CHAT_MODELS)}, scenarios: {len(SCENARIOS)}, "
          f"runs each: {RUNS_PER_PAIR}")
    print()
    header = (
        f"{'provider':<24} {'scenario':<16} {'wall (s)':>10} {'iters':>6} "
        f"{'in_tok':>7} {'out_tok':>8} {'tools':<40}"
    )
    print(header)
    print("-" * len(header))

    agg: dict[tuple[str, str], list[float]] = {}

    for provider in CHAT_MODELS:
        for scenario in SCENARIOS:
            walls: list[float] = []
            last = None
            for _ in range(RUNS_PER_PAIR):
                # Throttle: gateway limits to 20 RPM, и каждый turn это 1-3
                # iter'а tool-loop'а → ставим safety-buffer.
                time.sleep(INTER_CALL_SLEEP)
                last = _run_one_turn(provider, scenario)
                if last.get("error"):
                    print(f"{provider['label']:<24} {scenario['name']:<16}   "
                          f"ERR   {last['error'][:80]}")
                    break
                walls.append(last["wall_s"])
            if walls and last and "error" not in last:
                median = round(statistics.median(walls), 2)
                agg[(provider["label"], scenario["name"])] = walls
                tools_str = ",".join(last.get("tools", []))[:40]
                print(
                    f"{provider['label']:<24} {scenario['name']:<16} "
                    f"{median:>10} {last['iters']:>6} {last['in_tok']:>7} "
                    f"{last['out_tok']:>8} {tools_str:<40}"
                )
                fp = last.get("final_preview") or ""
                if fp:
                    # Windows cp1251 console крашится на emoji (✅ и т.п.).
                    # Принудительный ASCII с escape — нечитаемо, но не падает.
                    safe = fp[:160].encode("ascii", "backslashreplace").decode("ascii")
                    print(f"    reply: {safe!r}")

    print()
    print("---- median wall per (provider, scenario) ----")
    header2 = f"{'provider':<24}" + "".join(f" {s['name']:>16}" for s in SCENARIOS)
    print(header2)
    for provider in CHAT_MODELS:
        row = f"{provider['label']:<24}"
        for scenario in SCENARIOS:
            walls = agg.get((provider["label"], scenario["name"]))
            if walls:
                row += f" {statistics.median(walls):>16.2f}"
            else:
                row += f" {'ERR':>16}"
        print(row)
    return agg


# ---------------------------------------------------------------------------
# Whisper benchmark (TTS round-trip)
# ---------------------------------------------------------------------------


SAMPLES = [
    "Привет! Добавь молоко и хлеб в список покупок.",
    "Сохрани рецепт борща: свёкла, капуста, мясо, сметана. Варить на медленном огне сорок минут.",
    "Напомни мне завтра в девять утра забрать заказ.",
    "Что у меня сейчас запланировано на среду?",
    "Перехотел хлеб, убери из списка. И добавь яйца десять штук.",
]


async def _synthesise(text: str, out_path: Path) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural", rate="+0%")
    await communicate.save(str(out_path))


async def run_whisper_benchmark() -> None:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        print("[!] edge-tts not installed — run: pip install edge-tts")
        return

    import httpx

    tmp_dir = Path(".runtime/neuraldeep_probe")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    print()
    print("==== Whisper-1 (api.neuraldeep.ru) ====")
    print(f"{'#':<3} {'tts (s)':>8} {'stt (s)':>8} {'audio kb':>10} {'match':<10}  text")

    stt_durations: list[float] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for idx, ref in enumerate(SAMPLES, start=1):
            mp3_path = tmp_dir / f"sample_{idx}.mp3"

            t0 = time.monotonic()
            await _synthesise(ref, mp3_path)
            tts_dt = time.monotonic() - t0
            size_kb = mp3_path.stat().st_size / 1024

            audio = mp3_path.read_bytes()

            t0 = time.monotonic()
            try:
                resp = await client.post(
                    f"{BASE_URL}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    files={"file": (f"sample_{idx}.mp3", audio, "audio/mpeg")},
                    data={"model": WHISPER_MODEL, "language": "ru"},
                )
                stt_dt = time.monotonic() - t0
                if resp.status_code != 200:
                    print(f"{idx:<3} {tts_dt:>8.2f} {stt_dt:>8.2f} {size_kb:>10.1f} "
                          f"HTTP {resp.status_code}: {resp.text[:120]}")
                    continue
                got = (resp.json().get("text") or "").strip()
            except Exception as exc:
                stt_dt = time.monotonic() - t0
                print(f"{idx:<3} {tts_dt:>8.2f} {stt_dt:>8.2f} {size_kb:>10.1f} "
                      f"ERR: {type(exc).__name__}: {exc}")
                continue

            stt_durations.append(stt_dt)
            ref_norm = ref.strip().lower().rstrip(".!?")
            got_norm = got.strip().lower().rstrip(".!?")
            match = "EXACT" if got_norm == ref_norm else "close"
            print(f"{idx:<3} {tts_dt:>8.2f} {stt_dt:>8.2f} {size_kb:>10.1f} "
                  f"{match:<10}  ref: {ref[:60]}")
            print(f"{'':>32} {'':>10} got: {got[:60]}")

    if stt_durations:
        med = statistics.median(stt_durations)
        avg = statistics.mean(stt_durations)
        print()
        print(f"whisper-1 STT latency: median={med:.2f}s mean={avg:.2f}s "
              f"min={min(stt_durations):.2f}s max={max(stt_durations):.2f}s "
              f"(n={len(stt_durations)})")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


async def amain() -> int:
    run_chat_benchmark()
    await run_whisper_benchmark()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
