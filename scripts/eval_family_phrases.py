# -*- coding: utf-8 -*-
"""#133 — сквозной eval-прогон по семействам функций (ПО ТРЕБОВАНИЮ).

Замысел владельца (2026-06-13): на каждое семейство — фразы пользователя,
покрывающие его функционал. Каждая фраза идёт через ВЕСЬ боевой цикл с
ЖИВЫМИ LLM (мозг-планировщик + рот), замокан только канал. Ответ не
отправляется — собирается таблица:

    фраза → ответ Среды → что сделано в процессе → время каждого шага

Оцениваем глазами (Боря + Клод). Это НЕ pytest-гейт: живые LLM, сеть,
недетерминизм, деньги — запуск осознанный.

Запуск:
    .venv/Scripts/python.exe scripts/eval_family_phrases.py [--family shopping]
    [--out plans/eval-133-<date>.md]

Ключ мозга/рта берётся из SREDA_MIMO_API_KEY либо из ~/.qwen/settings.json
(тот же endpoint token-plan-sgp.xiaomimimo.com, что у прод-мозга).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path


# --- фразы по семействам (покрывают функционал: создать/показать/изменить/
#     удалить/уточнить). Расширяется. -----------------------------------------
FAMILY_PHRASES: dict[str, list[str]] = {
    "shopping": [
        "добавь в покупки молоко и хлеб",
        "что у меня в списке покупок",
        "купила молоко, убери из списка",
        "добавь 2 кг картошки",
    ],
    "reminders": [
        "напомни завтра в 9 позвонить маме",
        "какие у меня напоминания",
        "перенеси напоминание про маму на 10 утра",
        "отмени напоминание про маму",
    ],
    "checklists": [
        "запиши в дела на дачу: лопата, секатор, перчатки",
        "покажи дела",
        "отметь лопату сделанной",
    ],
    "tasks": [
        "добавь задачу позвонить врачу завтра",
        "покажи мои задачи",
        "выполнил задачу про врача",
    ],
    "recipes": [
        "сохрани рецепт борща: свёкла, капуста, варить 40 минут",
        "найди рецепт борща",
    ],
    "menu": [
        "составь меню на неделю",
        "покажи меню на эту неделю",
    ],
    "household": [
        "запомни: у меня дочь Маша 7 лет и муж Иван",
        "кто у меня в семье",
    ],
    "memory": [
        "запомни, что у нас аллергия на орехи",
        "что ты помнишь про мою семью",
    ],
    "web": [
        "какая погода завтра в Москве",
        "найди в интернете когда сажать рассаду томатов",
    ],
    "smalltalk_identity": [
        "привет",
        "что ты умеешь",
    ],
}


def _load_mimo_key() -> str | None:
    if os.environ.get("SREDA_MIMO_API_KEY"):
        return os.environ["SREDA_MIMO_API_KEY"]
    qwen = Path.home() / ".qwen" / "settings.json"
    try:
        d = json.loads(qwen.read_text(encoding="utf-8"))
        return d.get("env", {}).get("CUSTOM_API_KEY")
    except (OSError, ValueError):
        return None


TENANT = "tenant_eval"
CHAT_ID = "100000777"


class _CapturingChannel:
    """Фейк-канал: ловит исходящее, ничего не отправляет."""

    def __init__(self) -> None:
        self.sends: list[dict] = []
        self.edits: list[dict] = []
        self._mid = 5000

    async def send_message(self, **kw):
        self._mid += 1
        self.sends.append(dict(kw, _mid=self._mid))
        return {"ok": True, "result": {"message_id": self._mid, "date": 1}}

    async def edit_message_text(self, **kw):
        self.edits.append(dict(kw))
        return {"ok": True}

    async def delete_message(self, **kw):
        return {"ok": True}

    async def answer_callback_query(self, *a, **kw):
        return {"ok": True}

    def final_text(self) -> str:
        if self.edits:
            return self.edits[-1].get("text", "")
        return self.sends[-1].get("text", "") if self.sends else "(пусто)"


class _TraceCapture(logging.Handler):
    """Ловит блок sreda.trace последнего хода (шаги + тайминги)."""

    def __init__(self) -> None:
        super().__init__()
        self.last_block = ""

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "TRACE" in msg or "ms " in msg:
            self.last_block = msg


def _setup_env(tmp: Path, mimo_key: str) -> None:
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{(tmp / 'eval.db').as_posix()}"
    os.environ["SREDA_ENCRYPTION_KEY"] = key
    os.environ["SREDA_PLANNER_ENABLED_TENANTS"] = TENANT
    os.environ["SREDA_MESSAGE_QUEUE_ENABLED_TENANTS"] = ""
    os.environ["SREDA_TELEGRAM_BOT_TOKEN"] = "100:eval-token"
    os.environ["SREDA_TG_ACCOUNT_SALT"] = "f" * 64
    os.environ["SREDA_MIMO_API_KEY"] = mimo_key  # ЖИВОЙ мозг + рот
    os.environ["SREDA_FEATURE_MODULES"] = (
        "sreda_feature_housewife_assistant.plugin")
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
    os.environ["SREDA_COMPOSER_LLM_ENABLED_KEYS"] = ",".join(
        sorted(LLM_PROMPT_REGISTRY.prompt_keys()))


def _seed_tenant() -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import event
    from sreda.config.settings import get_settings
    from sreda.db.base import Base
    from sreda.db.session import get_engine, get_session_factory
    from sreda.features.app_registry import get_feature_registry
    import sreda.db.models  # noqa: F401
    import sreda.db.models.checklists  # noqa: F401
    import sreda.db.models.plan_library  # noqa: F401

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_feature_registry.cache_clear()
    engine = get_engine()

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")
        dbapi.execute("PRAGMA journal_mode=WAL")
        dbapi.execute("PRAGMA busy_timeout=15000")

    Base.metadata.create_all(engine)
    from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
    from sreda.db.models.core import Tenant, User, Workspace
    from sreda.db.models.user_profile import TenantUserProfile
    from sreda.services.housewife_onboarding import (
        STATUS_COMPLETE, HousewifeOnboardingService,
    )
    from sreda.services.onboarding import mark_welcome_sent
    now = datetime.now(timezone.utc)
    sess = get_session_factory()()
    try:
        sess.add(Tenant(id=TENANT, name="Eval",
                        approved_at=now))
        sess.add(Workspace(id="ws_eval", tenant_id=TENANT, name="W"))
        sess.add(User(id="user_eval", tenant_id=TENANT,
                      telegram_account_id=CHAT_ID))
        sess.add(TenantUserProfile(id="tup_eval", tenant_id=TENANT,
                                   user_id="user_eval", display_name="Боря",
                                   address_form="ty"))
        sess.add(SubscriptionPlan(id="plan_eval", plan_key="sreda_free",
                                  feature_key="housewife_assistant",
                                  title="Free", description="t", price_rub=0))
        sess.flush()
        sess.add(TenantSubscription(
            id="sub_eval", tenant_id=TENANT, plan_id="plan_eval",
            feature_key="housewife_assistant", status="active",
            starts_at=now - timedelta(days=1),
            active_until=now + timedelta(days=30)))
        sess.flush()
        mark_welcome_sent(sess, TENANT, "user_eval")
        ob = HousewifeOnboardingService(sess)
        st = ob.get_raw_state(tenant_id=TENANT, user_id="user_eval")
        st["status"] = STATUS_COMPLETE
        ob._persist(tenant_id=TENANT, user_id="user_eval", state=st,
                    source="system")
        sess.commit()
    finally:
        sess.close()


async def _run_phrase(app, channel: _CapturingChannel,
                      trace_cap: _TraceCapture, text: str,
                      update_id: int) -> dict:
    import httpx
    channel.sends.clear()
    channel.edits.clear()
    trace_cap.last_block = ""
    t0 = time.monotonic()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://eval") as client:
        await client.post("/webhooks/telegram/sreda", json={
            "update_id": update_id,
            "message": {"message_id": update_id * 10,
                        "chat": {"id": int(CHAT_ID), "type": "private"},
                        "text": text}})
    # дождаться отсоединённых задач хода
    pending = [t for t in asyncio.all_tasks()
               if t is not asyncio.current_task() and not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=60)
    total_ms = int((time.monotonic() - t0) * 1000)
    return {"phrase": text, "answer": channel.final_text(),
            "trace": trace_cap.last_block, "total_ms": total_ms}


def _steps_from_trace(block: str) -> str:
    """Вытащить «что сделано + время» из трасс-блока в одну строку."""
    import re
    steps = re.findall(r"(\d+)ms\s+([a-z][\w.]+)(?:\s+\[(\d+)ms\])?", block)
    out = []
    for _at, name, dur in steps:
        out.append(f"{name}({dur or '0'}ms)")
    return " → ".join(out) or "(нет трассы)"


async def _main_async(families: list[str], out_path: Path) -> None:
    mimo_key = _load_mimo_key()
    if not mimo_key:
        print("НЕТ ключа мозга: задай SREDA_MIMO_API_KEY или "
              "~/.qwen/settings.json env.CUSTOM_API_KEY", file=sys.stderr)
        sys.exit(2)

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="sreda-eval-"))
    _setup_env(tmp, mimo_key)
    _seed_tenant()

    # мокаем ТОЛЬКО канал; мозг и рот — ЖИВЫЕ
    channel = _CapturingChannel()
    import sreda.runtime.graph as graph_mod
    import sreda.services.telegram_inbound as ti
    ti.telegram_client_for = lambda *a, **k: channel  # type: ignore
    graph_mod.telegram_client_for = lambda *a, **k: channel  # type: ignore

    # биллинг-гейты глушим — eval про диалог, не про деньги (и SERIALIZABLE
    # UsageLedger на SQLite виснет). Мозг и рот при этом ОСТАЮТСЯ живыми.
    from sreda.services.entitlement_gate import EntitlementGate, GateResult
    EntitlementGate.check = lambda self, tenant_id: GateResult(  # type: ignore
        allowed=True, reason="ok", plan_key="sreda_free",
        is_grandfathered=False)
    from sreda.services.usage_ledger import UsageLedgerService
    UsageLedgerService.try_consume = (  # type: ignore
        lambda self, tenant_id, metric, amount, periods: True)

    from sreda.main import create_app
    app = create_app()  # вызывает configure_logging → dictConfig

    # обработчик трассы — ПОСЛЕ create_app (dictConfig иначе сбрасывает
    # внешние обработчики logger'а sreda.trace)
    trace_cap = _TraceCapture()
    _tl = logging.getLogger("sreda.trace")
    _tl.addHandler(trace_cap)
    _tl.setLevel(logging.INFO)

    rows: list[dict] = []
    uid = 1
    for fam in families:
        for phrase in FAMILY_PHRASES.get(fam, []):
            uid += 1
            try:
                r = await _run_phrase(app, channel, trace_cap, phrase, uid)
            except Exception as exc:  # noqa: BLE001
                r = {"phrase": phrase, "answer": f"ИСКЛЮЧЕНИЕ: {exc}",
                     "trace": "", "total_ms": 0}
            r["family"] = fam
            rows.append(r)
            print(f"[{fam}] {phrase!r} → {r['answer'][:80]!r} "
                  f"({r['total_ms']}ms)")

    # таблица
    lines = ["# Eval #133 — сквозной прогон по семействам (живые LLM)", "",
             "| Семейство | Фраза | Ответ Среды | Что сделано (шаги/время) | Итого |",
             "|---|---|---|---|---|"]
    for r in rows:
        ans = r["answer"].replace("\n", " ").replace("|", "\\|")[:300]
        steps = _steps_from_trace(r["trace"]).replace("|", "\\|")[:400]
        lines.append(f"| {r['family']} | {r['phrase']} | {ans} | "
                     f"{steps} | {r['total_ms']}ms |")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nТаблица: {out_path}  ({len(rows)} фраз)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", action="append",
                    help="семейство (повторяемо); по умолчанию все")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    families = args.family or list(FAMILY_PHRASES)
    out = Path(args.out) if args.out else Path("plans/eval-133-run.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_main_async(families, out))


if __name__ == "__main__":
    main()
