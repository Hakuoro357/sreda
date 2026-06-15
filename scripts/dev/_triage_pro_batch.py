"""Триаж дефект-классов диалога на ТЕКУЩЕМ коде + заданной модели.
Plan-level: гоняет сценарии через живой оркестратор, печатает по каждому
прогону success / tools / валидатор / флаги (filter-invention, misroute,
clarity). Provider из argv[1] (mimo-v2.5 | mimo-v2.5-pro), N из argv[2] (деф 5).

Запуск:
  python scripts/dev/_triage_pro_batch.py mimo-v2.5-pro 5
"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_PROVIDER = sys.argv[1] if len(sys.argv) > 1 else "mimo-v2.5-pro"
_N = int(sys.argv[2]) if len(sys.argv) > 2 else 5
_FILTER = sys.argv[3] if len(sys.argv) > 3 else None  # напр. "#146" — гнать только их

_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_DATABASE_URL"] = (
    f"sqlite:///{(Path(tempfile.gettempdir()) / 'triagebatch.db').as_posix()}"
)
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef"
).decode()
os.environ["SREDA_MIMO_API_KEY_FILE"] = str(_REPO / ".secrets" / "mimo_api_key.txt")
os.environ["SREDA_NVIDIA_API_KEY_FILE"] = str(_REPO / ".secrets" / "nvidia.txt")
os.environ["SREDA_TG_ACCOUNT_SALT"] = "x"
os.environ["SREDA_CHAT_PROVIDER"] = _PROVIDER
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sreda.config.settings import get_settings
get_settings.cache_clear()

from sreda.runtime.planner.few_shot_examples import render_few_shot_block
from sreda.runtime.planner.json_parse import parse_planner_json
from sreda.runtime.planner.orchestrator import PlannerContext, run as orchestrator_run
from sreda.runtime.planner.prompt_builder import (
    NowMoment, ProfileSnapshot, TurnMessage, TurnSnapshot,
)
from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

_MSK = ZoneInfo("Europe/Moscow")
_now = datetime.now(_MSK).replace(tzinfo=None)
_enabled = tuple(LLM_PROMPT_REGISTRY.prompt_keys())


def _turn(text: str, mins: int) -> TurnSnapshot:
    ts = (_now - timedelta(minutes=mins)).isoformat(timespec="seconds")
    return TurnSnapshot(turn_id=f"t{mins}", started_at=ts, summary=None,
                        messages=[TurnMessage(role="юзер", text=text, ts=ts)],
                        is_active=False)


# Сценарии триажа: (issue, message, closed_turns)
SIM = "добавь в покупки молоко, молоко 2.5%, молоко безлактозное, хлеб"
SCENARIOS = [
    ("#146", "сохрани рецепт борща: свёкла, капуста, картошка, варить 40 минут", ()),
    ("#146", "запиши рецепт: блины — мука, молоко, яйца, жарить на сковороде по 2 минуты", ()),
    ("#122", "удали молоко из списка", (_turn(SIM, 3),)),
    ("#122", "вычеркни второе", (_turn(SIM, 3),)),
    ("#125", "напомни", ()),
    ("#125", "добавь", ()),
    ("#106", "что ты умеешь?", ()),
    ("#106", "как приготовить борщ?", ()),
]


def _analyze(result):
    raw = result.raw_responses[-1] if result.raw_responses else ""
    flags = []
    tools = []
    try:
        payload = parse_planner_json(raw)
        acts = payload.get("actions", {})
        tools = [a.get("tool") for a in acts.values()] if isinstance(acts, dict) else []
        blob = json.dumps(payload, ensure_ascii=False)
        if ".filter(" in blob or ".where(" in blob:
            flags.append("ВЫДУМАН-filter/where")
        if "add_shopping_items" in tools:
            flags.append("→покупки")
        clar = payload.get("clarity") or payload.get("turn_classification", {}).get("clarity")
        if clar in ("needs_clarification", "ambiguous"):
            flags.append(f"clarity={clar}")
        if "save_recipes_batch" in tools:
            flags.append("save_recipes_batch")
    except Exception as exc:
        flags.append(f"JSON-fail:{str(exc)[:30]}")
    return tools, flags


async def _run_one(msg, closed):
    ctx = PlannerContext(
        tenant_id="t1", run_id="triage", feature_key="housewife_assistant",
        user_message=msg, voice_meta=None, now=NowMoment(_now),
        profile=ProfileSnapshot(address="ты"), memories=(), active_turn=None,
        closed_turns=closed, available_tools=tuple(MIGRATED_TOOL_SPECS),
        composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
        composer_llm_prompt_keys=_enabled,
        composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
        tool_registry_version="triage",
        few_shot_block=render_few_shot_block(effective_llm_keys=frozenset(_enabled)),
    )
    return await orchestrator_run(ctx, session_factory=None, admin_alert_fn=None)


async def _main():
    print(f"### ТРИАЖ provider={_PROVIDER} N={_N}")
    all_lat = []
    tot_succ = tot_runs = 0
    for issue, msg, closed in SCENARIOS:
        if _FILTER and _FILTER not in issue:
            continue
        succ = 0
        details = []
        lats = []
        for _ in range(_N):
            t0 = time.monotonic()
            r = await _run_one(msg, closed)
            dt = time.monotonic() - t0
            lats.append(dt); all_lat.append(dt)
            tot_runs += 1
            if r.success:
                succ += 1; tot_succ += 1
            tools, flags = _analyze(r)
            details.append(f"succ={r.success} {dt:.1f}s tools={tools} {flags}")
        avg = sum(lats) / len(lats) if lats else 0
        print(f"\n[{issue}] «{msg[:42]}» → success {succ}/{_N} | avg {avg:.1f}s")
        for d in details:
            print(f"    {d}")
    ov = sum(all_lat) / len(all_lat) if all_lat else 0
    mx = max(all_lat) if all_lat else 0
    print(f"\n### ИТОГ {_PROVIDER}: success {tot_succ}/{tot_runs} | avg {ov:.1f}s | max {mx:.1f}s")


asyncio.run(_main())
