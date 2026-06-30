#!/usr/bin/env python
"""#165 живой замер: A/B латентности Фредди и выбора инструмента — ПОЛНЫЕ vs ОБРЕЗАННЫЕ
описания инструментов. Один системный промпт в обоих прогонах → изолируем эффект обрезки.

Запуск на VDS (там Mercury-ключ), из временного чекаута ветки, прод-сервис НЕ трогаем:
  source /etc/sreda/.env  (или run_probe.sh-обёртка)
  PYTHONPATH=src .venv/bin/python scripts/measure_react_tool_compression.py

Меряет per-config: латентность p50/p95, выбранный первый инструмент, попадание в ожидаемую
семью; плюс A/B-согласие выбора (обрезка не должна менять выбор). Mercury-вызовы: 2 × N фраз.
"""
from __future__ import annotations

import asyncio
import statistics
import time

from langchain_core.messages import HumanMessage, SystemMessage

from sreda.config.settings import get_settings
from sreda.runtime import react_loop
from sreda.services.llm import get_chat_llm
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST

# (фраза, ожидаемая семья) — ~30 фраз по семьям; есть read-vs-write ловушки (правило #8).
CORPUS: list[tuple[str, str]] = [
    # напоминания
    ("напомни завтра в 9 утра позвонить маме", "reminders"),
    ("какие у меня сейчас напоминания", "reminders"),
    ("перенеси напоминание про зал на 18:00", "reminders"),
    # задачи
    ("добавь задачу полить цветы в субботу", "tasks"),
    ("покажи мои задачи на сегодня", "tasks"),
    ("отметь задачу про отчёт выполненной", "tasks"),
    # покупки
    ("добавь в список покупок молоко и хлеб", "shopping"),
    ("что у меня в списке покупок", "shopping"),
    ("отметь что купил яйца", "shopping"),
    # рецепты
    ("сохрани рецепт борща который я тебе сейчас продиктую", "recipes"),
    ("найди мой рецепт оладий", "recipes"),
    ("удали рецепт салата оливье из книги", "recipes"),
    # меню
    ("составь мне меню на неделю", "menu"),
    ("что у меня в меню на среду", "menu"),
    ("собери список покупок из моего меню", "menu"),
    # семья
    ("запиши что у меня жена и двое детей", "household"),
    ("кто у меня записан в семье", "household"),
    # чек-листы
    ("запиши в дела по машине: масло, фильтр, шиномонтаж", "checklists"),
    ("покажи мой список дел по ремонту", "checklists"),
    ("отметь пункт масло выполненным", "checklists"),
    # память
    ("запомни что я не ем глютен", "memory"),
    ("что я тебе рассказывал про мою работу", "memory"),
    # веб
    ("какая погода завтра в москве", "web"),
    ("поищи в интернете новости про курс рубля", "web"),
    # read-vs-write ловушки (правило #8: read НЕ должен звать write)
    ("у меня вообще есть список покупок?", "shopping"),
    ("покажи все мои рецепты", "recipes"),
    ("есть ли у меня напоминания на выходные", "reminders"),
    # многосемейная / разговорная
    ("отмени задачу про уборку и убери её из расписания", "tasks"),
    ("добавь рецепт и сразу запланируй его на ужин в пятницу", "recipes"),
    ("спасибо, ты супер", "_chat"),  # болтовня: ожидаем НИ ОДНОГО инструмента
]


def _build(trimmed: bool, session, tenant_id: str, user_id: str) -> list:
    """trimmed=True → обрезанные описания (как в проде после #165 Этап 1);
    trimmed=False → полные докстринги (как было до) — временно пустим реестр обрезки."""
    if trimmed:
        return react_loop.build_slice_tools(session, tenant_id, user_id)
    saved = react_loop._REACT_TOOL_DESC
    react_loop._REACT_TOOL_DESC = {}
    try:
        return react_loop.build_slice_tools(session, tenant_id, user_id)
    finally:
        react_loop._REACT_TOOL_DESC = saved


async def _ask(llm_tools, system: str, phrase: str):
    msgs = [SystemMessage(system), HumanMessage(phrase)]
    t0 = time.monotonic()
    try:
        resp = await llm_tools.ainvoke(msgs)
        dt = time.monotonic() - t0
        tcs = getattr(resp, "tool_calls", None) or []
        picked = tcs[0]["name"] if tcs else None
        return dt, picked, None
    except Exception as exc:  # noqa: BLE001
        return time.monotonic() - t0, None, type(exc).__name__


async def _run_config(label: str, tools, system: str):
    llm = get_chat_llm(provider=get_settings().planner_provider, settings=get_settings())
    bound = llm.bind_tools(tools)
    rows = []
    for phrase, exp_fam in CORPUS:
        dt, picked, err = await _ask(bound, system, phrase)
        fam = TOOL_FAMILY_MANIFEST.get(picked, "?") if picked else ("_chat" if not err else "ERR")
        rows.append((phrase, exp_fam, picked, fam, dt, err))
    return label, rows


def _report(label: str, rows, baseline_rows=None):
    lats = [r[4] for r in rows if r[5] is None]
    p50 = statistics.median(lats) if lats else 0
    p95 = (sorted(lats)[int(len(lats) * 0.95)] if len(lats) > 1 else (lats[0] if lats else 0))
    hit = sum(1 for r in rows if r[3] == r[1])
    errs = sum(1 for r in rows if r[5])
    print(f"\n===== {label}: N={len(rows)} =====")
    print(f"латентность p50={p50:.2f}s  p95={p95:.2f}s  ошибок={errs}")
    print(f"попадание в ожидаемую семью: {hit}/{len(rows)} ({100*hit//len(rows)}%)")
    if baseline_rows is not None:
        agree = sum(1 for a, b in zip(baseline_rows, rows) if a[2] == b[2])
        print(f"согласие выбора с ПОЛНЫМ набором: {agree}/{len(rows)} ({100*agree//len(rows)}%)")
        print("--- расхождения (фраза | полный→обрезанный) ---")
        for a, b in zip(baseline_rows, rows):
            if a[2] != b[2]:
                print(f"  «{a[0][:46]}» {a[2]} → {b[2]}")
    print("--- промахи мимо ожидаемой семьи ---")
    for r in rows:
        if r[3] != r[1]:
            print(f"  «{r[0][:46]}» ожид={r[1]} взял={r[2]}({r[3]}){' ERR='+r[5] if r[5] else ''}")


async def main():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    tid, uid = "tenant_probe", "user_probe"
    system = react_loop._system_prompt("2026-06-18 (Четверг)")

    full_tools = _build(False, session, tid, uid)
    trim_tools = _build(True, session, tid, uid)
    print(f"полный набор: {len(full_tools)} инстр.;  обрезанный: {len(trim_tools)} инстр.")

    _, full_rows = await _run_config("ПОЛНЫЕ описания (до #165)", full_tools, system)
    _report("ПОЛНЫЕ описания (до #165)", full_rows)
    _, trim_rows = await _run_config("ОБРЕЗАННЫЕ описания (после #165)", trim_tools, system)
    _report("ОБРЕЗАННЫЕ описания (после #165)", trim_rows, baseline_rows=full_rows)


if __name__ == "__main__":
    asyncio.run(main())
