"""#115 на НОВОМ механизме — реальные фразы через плановый контур локально.

Локальный аналог показ-раннера #87 (`plans/_vds_show_oldnew.py`), но записи
идут НАСТОЯЩИМИ инструментами #115 на локальной SQLite (живой okv2 с именами),
а сборка — живым голосом humanize_result (Ф4). Конвейер продовый:

    PlannerContext → orchestrator.run (живой MiMo, прод-промпт + few-shot)
      → ExecutionPlan → execute_plan (настоящие housewife/memory-инструменты;
        внешние типа web_search — мок как в июньском прогоне)
      → compose (живой LLM-голос humanize_result)

Прод не тронут. Запуск:

    .venv/Scripts/python.exe scripts/dev/live_115_planner_smoke.py \
        --input plans/_live115_phase1_failing.json \
        --out plans/_live115_planner_phase1.md --db live115_planner1.db

Вход: JSON [{"req": "...", "old": "...", "turn": "..."}]. PII-транскрипты —
в sreda/plans/_*.md (gitignored).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ARGS = argparse.ArgumentParser()
_ARGS.add_argument("--input", required=True)
_ARGS.add_argument("--out", required=True)
_ARGS.add_argument("--db", default="live115_planner.db")
_ARGS.add_argument("--history", type=int, default=6, help="скользящее окно истории, ходов")
CLI = _ARGS.parse_args()

# --- env ДО импорта sreda (settings кэшируются) -----------------------------
_REPO = Path(__file__).resolve().parents[2]
_DB_PATH = Path(tempfile.gettempdir()) / CLI.db
if _DB_PATH.exists():
    _DB_PATH.unlink()
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef"
).decode("ascii")
os.environ["SREDA_MIMO_API_KEY_FILE"] = str(_REPO / ".secrets" / "mimo_api_key.txt")
os.environ.setdefault("SREDA_ENV", "dev")
os.environ.setdefault("SREDA_TG_ACCOUNT_SALT", "live115_local_smoke_salt_not_prod")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY  # noqa: E402

# Включить ВЕСЬ реестр LLM-сборки (humanize_result и пр.) ДО первого get_settings —
# иначе выключатель по умолчанию гасит llm-голос и Ф4 не тестируется.
os.environ["SREDA_COMPOSER_LLM_ENABLED_KEYS"] = ",".join(LLM_PROMPT_REGISTRY.prompt_keys())

from sreda.config.settings import get_settings  # noqa: E402

get_settings.cache_clear()

from sreda.db.base import Base  # noqa: E402
from sreda.db.models import Assistant, Tenant, User, Workspace  # noqa: E402
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription  # noqa: E402
from sreda.db.session import get_engine, get_session_factory  # noqa: E402
from sreda.runtime.planner.executor import execute_plan  # noqa: E402
from sreda.runtime.planner.few_shot_examples import render_few_shot_block  # noqa: E402
from sreda.runtime.planner.orchestrator import (  # noqa: E402
    PlannerContext,
    run as orchestrator_run,
)
from sreda.runtime.planner.prompt_builder import (  # noqa: E402
    NowMoment,
    ProfileSnapshot,
    TurnMessage,
    TurnSnapshot,
)
from sreda.services.composer.compose import ComposerContext, compose  # noqa: E402
from sreda.services.composer.llm_composer import DEFAULT_LLM_COMPOSER  # noqa: E402
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY  # noqa: E402
from sreda.runtime.tools import build_memory_tools  # noqa: E402
from sreda.services.housewife_chat_tools import build_housewife_tools  # noqa: E402
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS  # noqa: E402

# моки только для дыр (web_search и пр.) — как в июньском показ-раннере
sys.path.insert(0, str(_REPO / "scripts" / "replay"))
from mock_tools import ReplayWriteRecorder, build_replay_tools_by_name  # noqa: E402

_MSK = ZoneInfo("Europe/Moscow")
TRANSCRIPT = Path(CLI.out) if os.path.isabs(CLI.out) else _REPO / CLI.out


class ConstantEmbeddingClient:
    dim = 8

    def embed_document(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 7

    def embed_query(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 7


def seed(session) -> None:
    from datetime import timedelta, timezone
    from uuid import uuid4

    session.add(Tenant(id="t1", name="T"))
    session.add(Workspace(id="w1", tenant_id="t1", name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id="t1", workspace_id="w1", name="Sreda"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=f"housewife_assistant_basic_{uuid4().hex[:8]}",
        feature_key="housewife_assistant",
        title="Housewife Basic",
        description="",
        price_rub=0,
        credits_monthly_quota=1_000_000_000,
    )
    session.add(plan)
    session.flush()
    session.add(
        TenantSubscription(
            id=f"sub_{uuid4().hex[:16]}",
            tenant_id="t1",
            plan_id=plan.id,
            feature_key="housewife_assistant",
            status="active",
            starts_at=datetime.now(timezone.utc) - timedelta(days=1),
            active_until=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    session.commit()


def _short(x, n=400):
    s = "" if x is None else str(x)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def make_ctx(user_message: str, run_id: str, closed: list[TurnSnapshot],
             few_shot: str, enabled_keys: tuple[str, ...]) -> PlannerContext:
    return PlannerContext(
        tenant_id="t1",
        run_id=run_id,
        feature_key="housewife_assistant",
        user_message=user_message,
        voice_meta=None,
        now=NowMoment(datetime.now(_MSK).replace(tzinfo=None)),
        profile=ProfileSnapshot(name="Катя"),
        memories=(),
        active_turn=None,
        closed_turns=tuple(closed),
        available_tools=tuple(MIGRATED_TOOL_SPECS),
        composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
        composer_llm_prompt_keys=enabled_keys,
        composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
        tool_registry_version="live-115-planner",
        few_shot_block=few_shot,
    )


def main() -> int:
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    seed(session)

    enabled_keys = tuple(LLM_PROMPT_REGISTRY.prompt_keys())
    few_shot = render_few_shot_block(effective_llm_keys=frozenset(enabled_keys))
    registry_map = {s.name: s for s in MIGRATED_TOOL_SPECS}

    # настоящие инструменты на локальной базе + моки для дыр
    real_tools = build_memory_tools(
        session=session, tenant_id="t1", user_id="u1",
        embedding_client=ConstantEmbeddingClient(),
    ) + build_housewife_tools(
        session=session, tenant_id="t1", user_id="u1",
        pending_buttons_state={}, menu_display_state={},
        embedding_client=ConstantEmbeddingClient(), bot_key="sreda",
    )
    tools_by_name = {t.name: t for t in real_tools}
    recorder = ReplayWriteRecorder()
    mock_pool = build_replay_tools_by_name(recorder)
    mocked_gaps = sorted(set(registry_map) - set(tools_by_name))
    for name in mocked_gaps:
        if name in mock_pool:
            tools_by_name[name] = mock_pool[name]

    records = json.loads(Path(CLI.input).read_text(encoding="utf-8"))
    lines: list[str] = [
        "# #115 на плановом контуре — реальные фразы",
        "",
        f"Дата: {datetime.now().isoformat(timespec='seconds')}. Вход: {CLI.input} "
        f"({len(records)} ходов). Планировщик+голос: живой MiMo. Инструменты: "
        f"настоящие на SQLite; мок только для: {', '.join(mocked_gaps) or '—'}. "
        "Прод не тронут.",
        "",
    ]

    def flush() -> None:
        TRANSCRIPT.write_text("\n".join(lines), encoding="utf-8")

    closed: list[TurnSnapshot] = []
    ok = 0
    for i, rec in enumerate(records, 1):
        phrase = rec["req"]
        suffix = f" (архивный ход {rec['turn']})" if rec.get("turn") else ""
        print(f"[{i}/{len(records)}] {phrase[:90]}", flush=True)
        lines.append(f"## Ход {i}{suffix}: «{phrase}»")
        if rec.get("old"):
            lines.append("")
            lines.append(f"**Старый ответ (прод, архив):** {rec['old']}")
        reply = None
        try:
            ctx = make_ctx(phrase, f"run_{i}", closed[-CLI.history:], few_shot, enabled_keys)
            result = asyncio.run(orchestrator_run(ctx, session_factory=None))
            if not result.success or result.execution_plan is None:
                lines.append("")
                lines.append(f"**⚠ ПЛАН НЕ ПОЛУЧЕН:** {_short(result.error_summary or result)}")
                lines.append("")
                flush()
                continue
            plan = result.plan
            acts = []
            for a in (getattr(plan, "actions", None) or []):
                args_s = json.dumps(getattr(a, "args", {}), ensure_ascii=False, default=str)
                acts.append(f"{getattr(a, 'tool', '?')}({_short(args_s, 300)})")
            comp = getattr(plan, "compose", None)
            comp_s = (f"kind={getattr(comp, 'kind', comp)} "
                      f"template={getattr(comp, 'template_id', None)} "
                      f"llm_key={getattr(comp, 'llm_prompt_key', None)}")
            lines.append("")
            lines.append(f"**План:** режим={getattr(plan, 'clarity', '?')}; "
                         f"действия: {('; '.join(acts)) or '(нет)'}; сборка: {comp_s}")

            exec_log = asyncio.run(
                execute_plan(result.execution_plan, tools_by_name, registry_map)
            )
            lines.append("")
            lines.append(f"**Выполнение:** итог={exec_log.outcome}")
            for st in exec_log.steps:
                raw_s = _short(st.raw_output, 350)
                lines.append(f"- {st.step_id}: `{st.tool}` → {st.status} | провод: `{raw_s}`")
                parsed = st.parsed_output
                if isinstance(parsed, dict) and parsed.get("display_summary"):
                    lines.append(f"  - display_summary: «{parsed['display_summary']}»")

            ctx2 = ComposerContext(
                tenant_id="t1", run_id=f"run_{i}", user_message=phrase,
                locale="ru-RU", timezone="Europe/Moscow",
            )
            reply = compose(
                plan.compose, exec_log,
                llm_composer=DEFAULT_LLM_COMPOSER,
                ctx=ctx2,
                expected_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
            ).text
            ok += 1
        except Exception:  # noqa: BLE001
            lines.append("")
            lines.append("**⚠ СБОЙ ХОДА:**")
            lines.append("```")
            lines.append(traceback.format_exc()[-1800:])
            lines.append("```")
        lines.append("")
        lines.append(f"**Новый ответ (плановый контур):** {reply or '(нет — см. сбой выше)'}")
        lines.append("")
        flush()
        if reply:
            print(f"    -> {reply[:110]}", flush=True)
        now_iso = datetime.now(_MSK).isoformat(timespec="seconds")
        closed.append(TurnSnapshot(
            turn_id=f"t{i}", started_at=now_iso, summary=None, is_active=False,
            messages=[
                TurnMessage(role="юзер", text=phrase, ts=now_iso),
                TurnMessage(role="среда", text=(reply or "(сбой)")[:500], ts=now_iso),
            ],
        ))

    lines.append(f"---\nИтог: {ok}/{len(records)} ходов дошли до ответа.")
    flush()
    print(f"\nГотово: {ok}/{len(records)}. Транскрипт: {TRANSCRIPT}", flush=True)
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
