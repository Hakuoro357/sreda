"""#124 — заземление на ТЕКУЩЕЙ модели: многоходовая диктовка «вещи в поездку»
из ИСТОРИИ (closed_turns) + просьба «собери в один текст с пунктами, чтобы
вычеркнуть». Воспроизводит класс провала 9/9 (сессия 755682022, 2026-06-10).

Провайдер берётся из argv[1] (mimo-v2.5 = light, mimo-v2.5-pro = pro) через
SREDA_CHAT_PROVIDER. Живой MiMo, прод-промпт, без записи в базу (temp SQLite,
session_factory=None → провайдер из settings.chat_provider).

Запуск:
  python scripts/dev/_124_multiturn_check.py mimo-v2.5
  python scripts/dev/_124_multiturn_check.py mimo-v2.5-pro
"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_PROVIDER = sys.argv[1] if len(sys.argv) > 1 else "mimo-v2.5"

_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_DATABASE_URL"] = (
    f"sqlite:///{(Path(tempfile.gettempdir()) / 'dbg124mt.db').as_posix()}"
)
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef"
).decode()
os.environ["SREDA_MIMO_API_KEY_FILE"] = str(_REPO / ".secrets" / "mimo_api_key.txt")
os.environ["SREDA_TG_ACCOUNT_SALT"] = "x"
os.environ["SREDA_CHAT_PROVIDER"] = _PROVIDER  # ← A/B по провайдеру
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sreda.config.settings import get_settings
get_settings.cache_clear()

from sreda.runtime.planner.few_shot_examples import render_few_shot_block
from sreda.runtime.planner.json_parse import parse_planner_json
from sreda.runtime.planner.orchestrator import PlannerContext, run as orchestrator_run
from sreda.runtime.planner.prompt_builder import (
    NowMoment, ProfileSnapshot, TurnMessage, TurnSnapshot,
)
from sreda.runtime.planner.schemas import Plan
from sreda.runtime.planner.validator import render_violations, validate_plan
from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

_MSK = ZoneInfo("Europe/Moscow")
_now = datetime.now(_MSK).replace(tzinfo=None)


def _turn(n: int, text: str, mins_ago: int) -> TurnSnapshot:
    ts = (_now - timedelta(minutes=mins_ago)).isoformat(timespec="seconds")
    return TurnSnapshot(
        turn_id=f"t{n}", started_at=ts, summary=None,
        messages=[TurnMessage(role="юзер", text=text, ts=ts)],
        is_active=False,
    )


# История диктовки (как в провальной сессии — вещи в поездку, многоходово).
CLOSED = (
    _turn(1, "Давай составим список вещей, которые мне нужны в поездку на "
              "выходные. Кроссовки, сандалии, несколько пар носков, штаны, куртка.", 6),
    _turn(2, "Ещё свитер, пару футболок и муслиновую рубашку с длинным рукавом.", 4),
    _turn(3, "Из косметички — шампунь, кондиционер, зубная паста.", 2),
)
# Текущий ход — прямая просьба собрать из истории, с маркером чек-листа.
CURRENT = ("Собери всё, что я надиктовала, в один текст. "
           "С пунктами, чтобы можно было вычеркнуть.")

enabled_keys = tuple(LLM_PROMPT_REGISTRY.prompt_keys())
ctx = PlannerContext(
    tenant_id="t1", run_id="dbg124mt", feature_key="housewife_assistant",
    user_message=CURRENT, voice_meta=None,
    now=NowMoment(_now),
    profile=ProfileSnapshot(address="ты"), memories=(), active_turn=None,
    closed_turns=CLOSED, available_tools=tuple(MIGRATED_TOOL_SPECS),
    composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
    composer_llm_prompt_keys=enabled_keys,
    composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
    tool_registry_version="dbg-124mt",
    few_shot_block=render_few_shot_block(effective_llm_keys=frozenset(enabled_keys)),
)

settings = get_settings()
print(f"### ПРОВАЙДЕР={_PROVIDER} (chat_provider={settings.chat_provider})")
result = asyncio.run(orchestrator_run(ctx, session_factory=None, admin_alert_fn=None))
print(f"== success={result.success} attempts={result.final_attempt_no} "
      f"error_summary={result.error_summary}")

# Разбор финального плана: какие инструменты, есть ли create_checklist/add_checklist_items,
# не ушло ли в покупки (add_shopping_items), валиден ли.
raw = result.raw_responses[-1] if result.raw_responses else ""
try:
    payload = parse_planner_json(raw)
    actions = payload.get("actions", {})
    tools = [a.get("tool") for a in actions.values()] if isinstance(actions, dict) else []
    print(f"-- инструменты плана: {tools}")
    has_checklist = any(t in ("create_checklist", "add_checklist_items") for t in tools)
    misroute_shopping = any(t == "add_shopping_items" for t in tools)
    print(f"-- чек-лист собран? {has_checklist} | мисроут в покупки? {misroute_shopping}")
    print(json.dumps(payload, ensure_ascii=False, indent=1)[:1500])
except Exception as exc:
    print(f"<JSON не разобрался: {exc}>\n{raw[:800]}")
