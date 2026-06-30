"""Выгрузить ПОЛНЫЙ планировочный промпт (#124 multi-turn, 70k префикс) в тело
Mercury chat/completions → /tmp/full_prompt.json. Затем: curl ... -d @файл.
"""
import base64, io, json, os, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{(Path(tempfile.gettempdir())/'dump.db').as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
os.environ["SREDA_TG_ACCOUNT_SALT"] = "x"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sreda.config.settings import get_settings
get_settings.cache_clear()
from sreda.runtime.planner.orchestrator import PlannerContext, _build_prompt_or_raise
from sreda.runtime.planner.prompt_builder import (
    NowMoment, ProfileSnapshot, TurnMessage, TurnSnapshot,
)
from sreda.runtime.planner.few_shot_examples import render_few_shot_block
from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

_MSK = ZoneInfo("Europe/Moscow")
_now = datetime.now(_MSK).replace(tzinfo=None)
_enabled = tuple(LLM_PROMPT_REGISTRY.prompt_keys())


def _turn(text, mins):
    ts = (_now - timedelta(minutes=mins)).isoformat(timespec="seconds")
    return TurnSnapshot(turn_id=f"t{mins}", started_at=ts, summary=None,
                        messages=[TurnMessage(role="юзер", text=text, ts=ts)], is_active=False)


CLOSED = (
    _turn("Давай составим список вещей в поездку на выходные. Кроссовки, сандалии, "
          "несколько пар носков, штаны, куртка.", 6),
    _turn("Ещё свитер, пару футболок и муслиновую рубашку с длинным рукавом.", 4),
    _turn("Из косметички — шампунь, кондиционер, зубная паста.", 2),
)
CURRENT = "Собери всё, что я надиктовала, в один текст. С пунктами, чтобы можно было вычеркнуть."

ctx = PlannerContext(
    tenant_id="t1", run_id="dump", feature_key="housewife_assistant",
    user_message=CURRENT, voice_meta=None, now=NowMoment(_now),
    profile=ProfileSnapshot(address="ты"), memories=(), active_turn=None,
    closed_turns=CLOSED, available_tools=tuple(MIGRATED_TOOL_SPECS),
    composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
    composer_llm_prompt_keys=_enabled,
    composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
    tool_registry_version="dump",
    few_shot_block=render_few_shot_block(effective_llm_keys=frozenset(_enabled)),
)

prompt = _build_prompt_or_raise(ctx, retry_feedback=None, composer_llm_prompt_keys=_enabled)
body = {"model": "mercury-2", "temperature": 0.1,
        "messages": [{"role": "user", "content": prompt}]}
out = Path(tempfile.gettempdir()) / "full_prompt.json"
out.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
txt = Path(tempfile.gettempdir()) / "full_prompt.txt"
txt.write_text(prompt, encoding="utf-8")  # сырой промпт (для вставки/просмотра)
print(f"написано: {out}")
print(f"сырой промпт: {txt}")
print(f"символов промпта: {len(prompt)} | ~токенов: {len(prompt)//3} | байт файла: {out.stat().st_size}")
