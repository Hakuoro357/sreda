"""#146 диагностика: почему save_recipe падает. Печатает по каждой попытке
сырой план + нарушения валидатора. Провайдер mimo-v2.5-pro.
"""
import asyncio, base64, io, json, os, sys, tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{(Path(tempfile.gettempdir()) / 'diag146.db').as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
os.environ["SREDA_MIMO_API_KEY_FILE"] = str(_REPO / ".secrets" / "mimo_api_key.txt")
os.environ["SREDA_TG_ACCOUNT_SALT"] = "x"
os.environ["SREDA_CHAT_PROVIDER"] = "mimo-v2.5-pro"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sreda.config.settings import get_settings
get_settings.cache_clear()
from sreda.runtime.planner.few_shot_examples import render_few_shot_block
from sreda.runtime.planner.json_parse import parse_planner_json
from sreda.runtime.planner.orchestrator import PlannerContext, run as orchestrator_run
from sreda.runtime.planner.prompt_builder import NowMoment, ProfileSnapshot
from sreda.runtime.planner.schemas import Plan
from sreda.runtime.planner.validator import render_violations, validate_plan
from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

_MSK = ZoneInfo("Europe/Moscow")
_enabled = tuple(LLM_PROMPT_REGISTRY.prompt_keys())
_reg = {s.name: s for s in MIGRATED_TOOL_SPECS}
MSG = "сохрани рецепт борща: свёкла, капуста, картошка, варить 40 минут"

async def _one(i):
    ctx = PlannerContext(
        tenant_id="t1", run_id=f"diag{i}", feature_key="housewife_assistant",
        user_message=MSG, voice_meta=None, now=NowMoment(datetime.now(_MSK).replace(tzinfo=None)),
        profile=ProfileSnapshot(address="ты"), memories=(), active_turn=None, closed_turns=(),
        available_tools=tuple(MIGRATED_TOOL_SPECS),
        composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
        composer_llm_prompt_keys=_enabled,
        composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
        tool_registry_version="diag146",
        few_shot_block=render_few_shot_block(effective_llm_keys=frozenset(_enabled)))
    r = await orchestrator_run(ctx, session_factory=None, admin_alert_fn=None)
    print(f"\n===== run {i}: success={r.success} attempts={r.final_attempt_no} err={r.error_summary}")
    raw = r.raw_responses[-1] if r.raw_responses else ""
    try:
        payload = parse_planner_json(raw)
    except Exception as e:
        print(f"  JSON-fail: {e}"); return
    try:
        plan = Plan.model_validate(payload)
    except Exception as e:
        print(f"  СХЕМА не прошла: {str(e)[:600]}")
        # печать save_recipe args
        acts = payload.get("actions", {})
        for sid, a in (acts.items() if isinstance(acts, dict) else []):
            if a.get("tool") == "save_recipe":
                print(f"  save_recipe args: {json.dumps(a.get('args', {}), ensure_ascii=False)[:500]}")
        return
    v = validate_plan(plan, registry=_reg,
                      composer_template_ids=frozenset(ctx.composer_template_ids),
                      composer_llm_prompt_keys=frozenset(_enabled),
                      llm_prompt_required_keys={k: LLM_PROMPT_REGISTRY.get(k).required_keys for k in _enabled})
    print(f"  нарушений: {len(v)}")
    for line in render_violations(v)[:6]:
        print("   *", line)

async def _main():
    for i in range(1, 6):
        await _one(i)

asyncio.run(_main())
