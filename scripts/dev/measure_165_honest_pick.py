#!/usr/bin/env python
"""#165 — ЧЕСТНЫЙ замер выбора инструмента: отделяем «вред обрезки» от «шума модели».

Мерило «24/34 совпало с полным» обманчиво: полный набор САМ нестабилен (Фредди отвечает по-разному
от прогона к прогону). Здесь меряем правильно:
  1. ТОЧНОСТЬ против ОЖИДАЕМОЙ семьи (а не против шумного полного набора) — для полного и обрезанного.
  2. ШУМ модели — гоняем каждый набор R раз, считаем самосогласие (доля фраз, где все R прогонов дали
     один и тот же инструмент). Это «пол шума».
Вердикт по фразе: correct (инструмент в ожид. семье) | recovery (need_family — легит. добор) |
  clarify (ask_human) | noaction (None) | wrong (инструмент НЕ той семьи).
∅-ожидание (болтовня/вне scope): correct = None или ask_human.

Запуск на VDS: sudo systemd-run --pipe ... /opt/sreda/.venv/bin/python scripts/dev/measure_165_honest_pick.py
Вызовов Mercury: 2 набора × R × N фраз. Результат дублируется в /tmp/m165_honest_result.txt.
"""
from __future__ import annotations

import asyncio
import collections
import statistics
import time

from langchain_core.messages import HumanMessage, SystemMessage

from sreda.config.settings import get_settings
from sreda.runtime import react_loop
from sreda.runtime.react_loop import (
    _FAMILY_ROOTS,
    _LAZY_FAMILIES,
    _PRUNABLE_FAMILIES,
    _route_families,
    _select_tools,
)
from sreda.services.llm import get_chat_llm
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST

R = 3  # прогонов на набор (для оценки шума)

# (фраза, ожидаемые семьи). Пустой кортеж = болтовня/вне scope (ждём НИ ОДНОГО инструмента).
CORPUS: list[tuple[str, tuple[str, ...]]] = [
    ("напомни завтра в 9 утра позвонить маме", ("reminders",)),
    ("какие у меня сейчас напоминания", ("reminders",)),
    ("перенеси напоминание про зал на 18:00", ("reminders",)),
    ("разбуди меня в 7 утра", ("reminders",)),
    ("добавь задачу полить цветы в субботу", ("tasks",)),
    ("покажи мои задачи на сегодня", ("tasks",)),
    ("отметь задачу про отчёт выполненной", ("tasks",)),
    ("запланируй сходить к врачу во вторник", ("tasks",)),
    ("добавь в список покупок молоко и хлеб", ("shopping",)),
    ("что у меня в списке покупок", ("shopping",)),
    ("отметь что купил яйца", ("shopping",)),
    ("надо заехать в магазин за сахаром", ("shopping",)),
    ("сохрани рецепт борща который я продиктую", ("recipes",)),
    ("найди мой рецепт оладий", ("recipes",)),
    ("удали рецепт салата оливье", ("recipes",)),
    ("покажи все мои рецепты", ("recipes",)),
    ("составь мне меню на неделю", ("menu",)),
    ("что у меня в меню на среду", ("menu",)),
    ("что приготовить на ужин в пятницу", ("menu",)),
    ("запиши что у меня жена и двое детей", ("household",)),
    ("кто у меня записан в семье", ("household",)),
    ("у дочки теперь день рождения в мае", ("household",)),
    ("запиши в дела по машине: масло, фильтр, шиномонтаж", ("checklists", "tasks")),
    ("покажи мой список дел по ремонту", ("checklists",)),
    ("отметь пункт масло выполненным", ("checklists",)),
    ("собери список что взять в поход", ("checklists",)),
    ("запомни что я не ем глютен", ("memory",)),
    ("что я тебе рассказывал про мою работу", ("memory",)),
    ("не забудь что я аллергик на орехи", ("memory",)),
    ("какая погода завтра в москве", ("web",)),
    ("поищи в интернете новости про курс рубля", ("web",)),
    ("сколько стоит билет на поезд до питера", ("web",)),
    ("добавь рецепт и сразу запланируй его на ужин в пятницу", ("recipes", "menu")),
    ("собери список покупок из моего меню", ("menu", "shopping")),
    ("отмени задачу про уборку и убери её в чек-лист", ("tasks", "checklists")),
    ("напомни купить подарок и добавь его в список покупок", ("reminders", "shopping")),
    ("спасибо, ты супер", ()),
    ("привет, как дела", ()),
    ("оплати за меня счёт за свет", ()),
]

_BESPOKE = {"ask_human": "clarify", "need_family": "recovery", "link_task": "tasks",
            "unlink_task": "tasks", "delete_my_account": "_account"}


def _pruned_tools(all_tools: list, text: str) -> list:
    routed = set(_route_families(text, k=len(_FAMILY_ROOTS)))
    base = sorted((set(_LAZY_FAMILIES) - _PRUNABLE_FAMILIES) | (routed & _PRUNABLE_FAMILIES))
    return _select_tools(all_tools, base)


def _verdict(pick: str | None, expected: tuple[str, ...]) -> str:
    if pick is None:
        return "correct" if not expected else "noaction"
    if pick == "ask_human":
        return "correct" if not expected else "clarify"
    if pick == "need_family":
        return "recovery"
    fam = TOOL_FAMILY_MANIFEST.get(pick) or _BESPOKE.get(pick, "?")
    if not expected:
        return "wrong"  # позвал инструмент на болтовню/вне scope
    return "correct" if fam in expected else "wrong"


async def _ask(bound, system: str, phrase: str) -> str | None:
    msgs = [SystemMessage(system), HumanMessage(phrase)]
    try:
        resp = await bound.ainvoke(msgs)
        tcs = getattr(resp, "tool_calls", None) or []
        return tcs[0]["name"] if tcs else None
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{type(exc).__name__}"


async def main():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import sreda.db.models  # noqa: F401
    from sreda.db.base import Base

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    all_tools = react_loop.build_slice_tools(session, "tenant_probe", "user_probe")
    system = react_loop._system_prompt("2026-06-24 (Вторник)")
    settings = get_settings()
    llm = get_chat_llm(provider=settings.planner_provider, settings=settings)
    full_bound = llm.bind_tools(all_tools)

    # picks[config][run][phrase] = tool name
    picks: dict[str, list[dict[str, str | None]]] = {"full": [], "pruned": []}
    t0 = time.monotonic()
    for run in range(R):
        full_run, prn_run = {}, {}
        for ph, _exp in CORPUS:
            full_run[ph] = await _ask(full_bound, system, ph)
            prn_run[ph] = await _ask(llm.bind_tools(_pruned_tools(all_tools, ph)), system, ph)
        picks["full"].append(full_run)
        picks["pruned"].append(prn_run)
    dur = time.monotonic() - t0

    lines: list[str] = []

    def out(s=""):
        print(s)
        lines.append(s)

    out(f"planner={settings.planner_provider}; N={len(CORPUS)}; R={R}; время={dur:.0f}с")

    # 1. Точность против ОЖИДАЕМОЙ семьи (среднее по R прогонам)
    out("\n===== ТОЧНОСТЬ против ПРАВИЛЬНОГО ОТВЕТА (среднее по 3 прогонам) =====")
    for cfg in ("full", "pruned"):
        agg = collections.Counter()
        for run in picks[cfg]:
            for ph, exp in CORPUS:
                agg[_verdict(run[ph], exp)] += 1
        tot = len(CORPUS) * R
        good = agg["correct"] + agg["recovery"]
        out(f"  {cfg:6}: correct={agg['correct']/R:.1f}/{len(CORPUS)}  recovery={agg['recovery']/R:.1f}  "
            f"clarify={agg['clarify']/R:.1f}  noaction={agg['noaction']/R:.1f}  WRONG={agg['wrong']/R:.1f}  "
            f"| good(correct+recovery)={100*good//tot}%")

    # 2. Шум модели: самосогласие набора (доля фраз, где все R прогонов дали один инструмент)
    out("\n===== ШУМ МОДЕЛИ (самосогласие: одинаков ли выбор во всех 3 прогонах) =====")
    for cfg in ("full", "pruned"):
        stable = sum(1 for ph, _ in CORPUS if len({picks[cfg][r][ph] for r in range(R)}) == 1)
        out(f"  {cfg:6}: стабильных {stable}/{len(CORPUS)} ({100*stable//len(CORPUS)}%)  "
            f"→ шум на {len(CORPUS)-stable} фразах")

    # 3. Старое «совпало с полным» (прогон 1) — для сравнения с пунктом 2
    same = sum(1 for ph, _ in CORPUS if picks["full"][0][ph] == picks["pruned"][0][ph])
    ff = sum(1 for ph, _ in CORPUS if picks["full"][0][ph] == picks["full"][1][ph])
    out(f"\n===== «СОВПАЛО» (прогон 1) =====")
    out(f"  обрезанный vs полный: {same}/{len(CORPUS)}")
    out(f"  полный vs полный (прогон1 vs прогон2, ПОЛ ШУМА): {ff}/{len(CORPUS)}")
    out("  → если эти два близки — расхождение обрезки в пределах шума модели")

    # 4. Где обрезанный реально ХУЖЕ: wrong при обрезке, но correct/recovery на полном (в большинстве прогонов)
    out("\n===== ФРАЗЫ С РЕАЛЬНЫМ ВРЕДОМ ОБРЕЗКИ (pruned WRONG, full не WRONG) =====")
    found = False
    for ph, exp in CORPUS:
        pv = [_verdict(picks["pruned"][r][ph], exp) for r in range(R)]
        fv = [_verdict(picks["full"][r][ph], exp) for r in range(R)]
        pruned_bad = pv.count("wrong") >= 2
        full_ok = fv.count("wrong") <= 1
        if pruned_bad and full_ok:
            found = True
            out(f"  🔴 «{ph[:46]}» ожид={exp} pruned={[picks['pruned'][r][ph] for r in range(R)]} "
                f"full={[picks['full'][r][ph] for r in range(R)]}")
    if not found:
        out("  (нет фраз, где обрезка стабильно портит то, что полный набор делал верно)")

    with open("/tmp/m165_honest_result.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n[результат также записан в /tmp/m165_honest_result.txt]")


if __name__ == "__main__":
    asyncio.run(main())
