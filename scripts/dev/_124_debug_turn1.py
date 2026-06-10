"""#124 — отладка хода 1 провальной сессии: какой план выдал планировщик
и какие проверки его зарубили. Живой MiMo, прод-промпт, без записи в базу.

Реплика ниже — СИНТЕТИЧЕСКАЯ, по мотивам прод-сессии 2026-06-10 (#124):
диктовка «вещи в поездку» того же класса. Ловушка воспроизводится на любом
такого рода входе: модель кладёт items объектами {"title": …} вместо строк.
"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_DATABASE_URL"] = (
    f"sqlite:///{(Path(tempfile.gettempdir()) / 'dbg124.db').as_posix()}"
)
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef"
).decode()
os.environ["SREDA_MIMO_API_KEY_FILE"] = str(_REPO / ".secrets" / "mimo_api_key.txt")
os.environ["SREDA_TG_ACCOUNT_SALT"] = "x"
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
TURN1 = (
    "Давай составим список вещей, которые мне нужны в поездку на выходные. "
    "Значит, кроссовки, сандалии, несколько пар носков, штаны, куртка, "
    "свитер, пару футболок, наверное."
)

enabled_keys = tuple(LLM_PROMPT_REGISTRY.prompt_keys())
ctx = PlannerContext(
    tenant_id="t1", run_id="dbg124", feature_key="housewife_assistant",
    user_message=TURN1, voice_meta=None,
    now=NowMoment(datetime.now(_MSK).replace(tzinfo=None)),
    profile=ProfileSnapshot(address="ты"), memories=(), active_turn=None,
    closed_turns=(), available_tools=tuple(MIGRATED_TOOL_SPECS),
    composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
    composer_llm_prompt_keys=enabled_keys,
    composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
    tool_registry_version="dbg-124",
    few_shot_block=render_few_shot_block(
        effective_llm_keys=frozenset(enabled_keys)
    ),
)

result = asyncio.run(orchestrator_run(ctx, session_factory=None, admin_alert_fn=None))
print(f"== success={result.success} attempts={result.final_attempt_no} "
      f"error_summary={result.error_summary}")

registry_map = {s.name: s for s in MIGRATED_TOOL_SPECS}
for i, raw in enumerate(result.raw_responses, 1):
    print(f"\n===== ПОПЫТКА {i}: сырой ответ планировщика =====")
    try:
        payload = parse_planner_json(raw)
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:2600])
    except Exception as exc:
        print(f"<JSON не разобрался: {exc}>")
        print(raw[:1500])
        continue
    try:
        plan = Plan.model_validate(payload)
    except Exception as exc:
        print(f"\n-- схема не прошла: {str(exc)[:800]}")
        continue
    violations = validate_plan(
        plan, registry=registry_map,
        composer_template_ids=frozenset(ctx.composer_template_ids),
        composer_llm_prompt_keys=frozenset(enabled_keys),
        llm_prompt_required_keys={
            k: LLM_PROMPT_REGISTRY.get(k).required_keys for k in enabled_keys
        },
    )
    print(f"\n-- нарушений валидатора: {len(violations)}")
    for line in render_violations(violations)[:8]:
        print("   *", line)
