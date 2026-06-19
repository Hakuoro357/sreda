"""Классифицировать 6 succ=False из полного прогона Mercury: реальная ошибка
планировщика vs серверная 500 (пустой ответ) vs ложный needs_clarification.
Печатает по каждой фразе: success, попытки, error_summary, длины raw-ответов
(0 = HTTP-ошибка/пусто), нарушения валидатора на 1-й попытке."""
import asyncio, base64, io, os, sys, tempfile, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_PROV = "inception-mercury2"
_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{(Path(tempfile.gettempdir())/'clsfail.db').as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
os.environ["SREDA_INCEPTION_API_KEY_FILE"] = str(_REPO/".secrets"/"inception.txt")
os.environ["SREDA_TG_ACCOUNT_SALT"] = "x"
os.environ["SREDA_PLANNER_PROVIDER"] = _PROV  # ← планировщик берёт ЕЁ
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
from sreda.config.settings import get_settings; get_settings.cache_clear()

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

FAILS = [
    ("tasks", "поставь задачу оплатить интернет сегодня"),
    ("tasks", "покажи мои задачи"),
    ("recipes", "найди рецепт борща"),
    ("menu", "покажи меню на эту неделю"),
    ("memory", "что ты знаешь про наши аллергии"),
    ("web", "какая погода завтра в Москве"),
]


def _ctx(msg):
    return PlannerContext(
        tenant_id="t1", run_id="cls", feature_key="housewife_assistant", user_message=msg,
        voice_meta=None, now=NowMoment(_now), profile=ProfileSnapshot(address="ты"),
        memories=(), active_turn=None, closed_turns=(), available_tools=tuple(MIGRATED_TOOL_SPECS),
        composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()), composer_llm_prompt_keys=_enabled,
        composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(), tool_registry_version="cls",
        few_shot_block=None)


async def _one(fam, msg):
    t0 = time.monotonic()
    r = await orchestrator_run(_ctx(msg), session_factory=None, admin_alert_fn=None)
    dt = time.monotonic()-t0
    print(f"\n[{fam}] «{msg}»")
    print(f"  success={r.success} | попыток={r.final_attempt_no} | {dt:.1f}с | err={r.error_summary}")
    raws = r.raw_responses or ()
    for i, raw in enumerate(raws, 1):
        rl = len(raw or "")
        tag = "ПУСТО(HTTP/500?)" if rl == 0 else f"{rl} симв"
        print(f"  попытка {i}: ответ {tag}")
        if rl:
            try:
                payload = parse_planner_json(raw); plan = Plan.model_validate(payload)
                v = validate_plan(plan, registry=_reg,
                    composer_template_ids=frozenset(_enabled and COMPOSER_REGISTRY.template_ids()),
                    composer_llm_prompt_keys=frozenset(_enabled),
                    llm_prompt_required_keys={k: LLM_PROMPT_REGISTRY.get(k).required_keys for k in _enabled})
                print(f"     нарушений валидатора: {len(v)}")
                for line in render_violations(v)[:6]:
                    print("      *", line)
            except Exception as e:
                print(f"     JSON/схема не прошла: {str(e)[:160]}")


async def _main():
    print(f"### КЛАССИФИКАЦИЯ 6 провалов | planner={get_settings().planner_provider}")
    for fam, msg in FAILS:
        try:
            await _one(fam, msg)
        except Exception as e:
            print(f"\n[{fam}] «{msg}» → ИСКЛЮЧЕНИЕ: {type(e).__name__}: {str(e)[:160]}")
        await asyncio.sleep(4)  # троттл под лимит токенов/мин

asyncio.run(_main())
