#!/usr/bin/env python
"""#165 часть 2 — A/B латентности Фредди: ПОЛНЫЙ бинд (Этап 1, ~8.5k ток) vs ОБРЕЗАННЫЙ
per-turn набор (Этап 2, ~2.4-4.7k ток). Один системный промпт, один корпус, реальный Mercury.
Изолирует эффект РАЗМЕРА набора инструментов на скорость хода (роутер-словарь — ~0 латентности).

Запуск на VDS (там Mercury-ключ), из temp-worktree, прод-сервис НЕ трогаем:
  sudo systemd-run --pipe --collect --quiet -p EnvironmentFile=/etc/sreda/.env -p User=sreda \
    -p WorkingDirectory=<wt> --setenv=PYTHONPATH=<wt>/src /opt/sreda/.venv/bin/python \
    scripts/dev/measure_165_latency_ab.py

Mercury-вызовов: 2 × N фраз (полный + обрезанный). Read-only: только bind_tools+ainvoke, без графа/побочек.
"""
from __future__ import annotations

import asyncio
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

CORPUS: list[str] = [
    "напомни завтра в 9 утра позвонить маме",
    "какие у меня сейчас напоминания",
    "перенеси напоминание про зал на 18:00",
    "разбуди меня в 7 утра",
    "добавь задачу полить цветы в субботу",
    "покажи мои задачи на сегодня",
    "отметь задачу про отчёт выполненной",
    "запланируй сходить к врачу во вторник",
    "добавь в список покупок молоко и хлеб",
    "что у меня в списке покупок",
    "отметь что купил яйца",
    "надо заехать в магазин за сахаром",
    "сохрани рецепт борща который я продиктую",
    "найди мой рецепт оладий",
    "удали рецепт салата оливье",
    "покажи все мои рецепты",
    "составь мне меню на неделю",
    "что у меня в меню на среду",
    "что приготовить на ужин в пятницу",
    "запиши что у меня жена и двое детей",
    "кто у меня записан в семье",
    "запиши в дела по машине: масло, фильтр, шиномонтаж",
    "покажи мой список дел по ремонту",
    "отметь пункт масло выполненным",
    "собери список что взять в поход",
    "запомни что я не ем глютен",
    "что я тебе рассказывал про мою работу",
    "какая погода завтра в москве",
    "поищи в интернете новости про курс рубля",
    "добавь рецепт и сразу запланируй его на ужин в пятницу",
    "собери список покупок из моего меню",
    "напомни купить подарок и добавь его в список покупок",
    "спасибо, ты супер",
    "оплати за меня счёт за свет",
]


def _pruned_tools(all_tools: list, text: str) -> list:
    routed = set(_route_families(text, k=len(_FAMILY_ROOTS)))
    base = sorted((set(_LAZY_FAMILIES) - _PRUNABLE_FAMILIES) | (routed & _PRUNABLE_FAMILIES))
    return _select_tools(all_tools, base)


async def _ask(bound, system: str, phrase: str):
    msgs = [SystemMessage(system), HumanMessage(phrase)]
    t0 = time.monotonic()
    try:
        resp = await bound.ainvoke(msgs)
        dt = time.monotonic() - t0
        tcs = getattr(resp, "tool_calls", None) or []
        return dt, (tcs[0]["name"] if tcs else None), None
    except Exception as exc:  # noqa: BLE001
        return time.monotonic() - t0, None, type(exc).__name__


def _pctl(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * q))] if s else 0.0


async def main():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import sreda.db.models  # noqa: F401
    from sreda.db.base import Base

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    tid, uid = "tenant_probe", "user_probe"

    all_tools = react_loop.build_slice_tools(session, tid, uid)
    system = react_loop._system_prompt("2026-06-24 (Вторник)")
    settings = get_settings()
    llm = get_chat_llm(provider=settings.planner_provider, settings=settings)
    print(f"planner={settings.planner_provider}; инструментов всего={len(all_tools)}")

    full_bound = llm.bind_tools(all_tools)
    full_lat, prn_lat = [], []
    full_pick, prn_pick = {}, {}
    rows = []
    for ph in CORPUS:
        df, pf, ef = await _ask(full_bound, system, ph)
        prn_tools = _pruned_tools(all_tools, ph)
        dp, pp, ep = await _ask(llm.bind_tools(prn_tools), system, ph)
        if ef is None:
            full_lat.append(df)
        if ep is None:
            prn_lat.append(dp)
        full_pick[ph] = pf
        prn_pick[ph] = pp
        rows.append((ph, len(prn_tools), df, pf, ef, dp, pp, ep))

    def rep(label, lat):
        if not lat:
            print(f"{label}: нет успешных вызовов")
            return
        print(f"{label}: p50={statistics.median(lat):.2f}s  p95={_pctl(lat, 0.95):.2f}s  "
              f"mean={statistics.mean(lat):.2f}s  n={len(lat)}")

    print("\n===== ЛАТЕНТНОСТЬ Фредди =====")
    rep("ПОЛНЫЙ бинд (Этап 1, ~8.5k ток)", full_lat)
    rep("ОБРЕЗАННЫЙ per-turn (Этап 2, ~2.4-4.7k)", prn_lat)
    if full_lat and prn_lat:
        d = statistics.median(full_lat) - statistics.median(prn_lat)
        print(f"Δ p50 = {d:.2f}s ({100 * d / statistics.median(full_lat):.0f}% быстрее обрезанный)")

    # согласие выбора: при ОБРЕЗКЕ модель либо берёт тот же инструмент, либо need_family (легит. recovery)
    agree = sum(1 for ph in CORPUS if full_pick[ph] == prn_pick[ph])
    nf = sum(1 for ph in CORPUS if prn_pick[ph] == "need_family")
    print("\n===== ВЫБОР ИНСТРУМЕНТА (full → pruned) =====")
    print(f"совпал: {agree}/{len(CORPUS)}; need_family (recovery) при обрезке: {nf}")
    print("--- расхождения ---")
    for ph, ntools, df, pf, ef, dp, pp, ep in rows:
        if pf != pp or ef or ep:
            fam = TOOL_FAMILY_MANIFEST.get(pp, "?") if pp else "—"
            print(f"  «{ph[:42]}» full={pf} → pruned={pp}({fam}) [{ntools} инстр]"
                  f"{' ERRf=' + ef if ef else ''}{' ERRp=' + ep if ep else ''}")


if __name__ == "__main__":
    asyncio.run(main())
