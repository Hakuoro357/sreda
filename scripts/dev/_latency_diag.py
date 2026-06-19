"""Почему тормозит новый механизм: разложить ОДИН ход по слоям.
Тайминг build_prompt / per-attempt invoke / validate, число попыток, и ЧТО
отверг валидатор на 1-й попытке (корень ретрая). Провайдер argv[1] (быстрый
Mercury — чтобы видеть оверхед механизма, не тормоза модели)."""
import asyncio, base64, io, os, sys, tempfile, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_PROV = sys.argv[1] if len(sys.argv) > 1 else "inception-mercury2"
_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{(Path(tempfile.gettempdir())/'latdiag.db').as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
os.environ["SREDA_INCEPTION_API_KEY_FILE"] = str(_REPO/".secrets"/"inception.txt")
os.environ["SREDA_TG_ACCOUNT_SALT"] = "x"
os.environ["SREDA_CHAT_PROVIDER"] = _PROV
os.environ["SREDA_PLANNER_PROVIDER"] = _PROV  # ← ВЕРНАЯ переменная: планировщик берёт ЕЁ
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sreda.config.settings import get_settings; get_settings.cache_clear()

import sreda.runtime.planner.llm as planner_llm
from sreda.runtime.planner.json_parse import parse_planner_json
from sreda.runtime.planner.orchestrator import PlannerContext, run as orchestrator_run
from sreda.runtime.planner.prompt_builder import NowMoment, ProfileSnapshot
from sreda.runtime.planner.schemas import Plan
from sreda.runtime.planner.validator import render_violations, validate_plan
from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

_MSK = ZoneInfo("Europe/Moscow"); _now = datetime.now(_MSK).replace(tzinfo=None)
_enabled = tuple(LLM_PROMPT_REGISTRY.prompt_keys()); _reg = {s.name: s for s in MIGRATED_TOOL_SPECS}

# Перехватываем call_planner, чтобы замерить КАЖДУЮ попытку (invoke).
_orig = planner_llm.call_planner
_calls = []
def _timed(prompt, **kw):
    t0 = time.monotonic(); r = _orig(prompt, **kw); dt = time.monotonic()-t0
    _calls.append((dt, len(prompt))); return r
planner_llm.call_planner = _timed
import sreda.runtime.planner.orchestrator as orch
orch.call_planner = _timed

MSG = "сохрани рецепт борща: свёкла, капуста, картошка, варить 40 минут"
ctx = PlannerContext(
    tenant_id="t1", run_id="lat", feature_key="housewife_assistant", user_message=MSG,
    voice_meta=None, now=NowMoment(_now), profile=ProfileSnapshot(address="ты"),
    memories=(), active_turn=None, closed_turns=(), available_tools=tuple(MIGRATED_TOOL_SPECS),
    composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()), composer_llm_prompt_keys=_enabled,
    composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(), tool_registry_version="lat",
    few_shot_block=None)

async def _main():
    t0 = time.monotonic()
    r = await orchestrator_run(ctx, session_factory=None, admin_alert_fn=None)
    total = time.monotonic()-t0
    print(f"### provider={_PROV}")
    print(f"ход ВСЕГО: {total:.1f}с | success={r.success} | попыток={r.final_attempt_no} | err={r.error_summary}")
    for i,(dt,plen) in enumerate(_calls,1):
        print(f"  попытка {i}: invoke {dt:.1f}с (промпт {plen} симв)")
    for i,raw in enumerate(r.raw_responses,1):
        print(f"  --- попытка {i}: разбор+валидатор ---")
        try:
            payload = parse_planner_json(raw); plan = Plan.model_validate(payload)
            v = validate_plan(plan, registry=_reg,
                composer_template_ids=frozenset(ctx.composer_template_ids),
                composer_llm_prompt_keys=frozenset(_enabled),
                llm_prompt_required_keys={k: LLM_PROMPT_REGISTRY.get(k).required_keys for k in _enabled})
            print(f"     нарушений: {len(v)}")
            for line in render_violations(v)[:5]: print("      *", line)
        except Exception as e:
            print(f"     JSON/схема не прошла: {str(e)[:200]}")

asyncio.run(_main())
