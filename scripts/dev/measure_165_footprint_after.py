#!/usr/bin/env python
"""#165 — футпринт инструментов ПОСЛЕ обрезки (офлайн, детерминированно, БЕЗ сети/прода).

Считает токены набора, который реально биндится Фредди на ход, ТЕМ ЖЕ мерилом, что Этап 0
(convert_to_openai_tool → JSON → tiktoken cl100k-прокси; абсолют для Mercury иной, но
относительное сравнение до/после корректно — см. plans/165-baseline.md).

  before  — все инструменты, ПОЛНЫЕ описания (_REACT_TOOL_DESC={})           → baseline Этап 0 (~22.6k)
  stage1  — все инструменты, ОБРЕЗАННЫЕ (как на проде), full-bind            → Этап 1 (~7.7k)
  floor   — ход БЕЗ совпадения prunable-семьи: core + непрунабельная-lazy    → минимум пер-ход
  pruned  — per-turn на проде: base_fams route-узла (Этап 2) на корпусе      → реальный эффект

Роутер — словарь (_route_families), сети нет. Запуск из worktree:
  PYTHONPATH=src python scripts/dev/measure_165_footprint_after.py
"""
from __future__ import annotations

import json
import statistics

import tiktoken
from langchain_core.utils.function_calling import convert_to_openai_tool

from sreda.runtime import react_loop
from sreda.runtime.react_loop import (
    _FAMILY_ROOTS,
    _LAZY_FAMILIES,
    _PRUNABLE_FAMILIES,
    _route_families,
    _select_tools,
)

_ENC = tiktoken.get_encoding("cl100k_base")

# Репрезентативный корпус русских фраз (из scripts/measure_family_router.py): все семьи + смешанные + болтовня.
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
    "у дочки теперь день рождения в мае",
    "запиши в дела по машине: масло, фильтр, шиномонтаж",
    "покажи мой список дел по ремонту",
    "отметь пункт масло выполненным",
    "собери список что взять в поход",
    "запомни что я не ем глютен",
    "что я тебе рассказывал про мою работу",
    "не забудь что я аллергик на орехи",
    "какая погода завтра в москве",
    "поищи в интернете новости про курс рубля",
    "сколько стоит билет на поезд до питера",
    "добавь рецепт и сразу запланируй его на ужин в пятницу",
    "собери список покупок из моего меню",
    "отмени задачу про уборку и убери её в чек-лист",
    "напомни купить подарок и добавь его в список покупок",
    "спасибо, ты супер",
    "привет, как дела",
    "оплати за меня счёт за свет",
]


def tok(tools: list) -> int:
    total = 0
    for t in tools:
        spec = convert_to_openai_tool(t)
        total += len(_ENC.encode(json.dumps(spec, ensure_ascii=False)))
    return total


def build(session, tid: str, uid: str, full: bool = False) -> list:
    if not full:
        return react_loop.build_slice_tools(session, tid, uid)
    saved = react_loop._REACT_TOOL_DESC
    react_loop._REACT_TOOL_DESC = {}
    try:
        return react_loop.build_slice_tools(session, tid, uid)
    finally:
        react_loop._REACT_TOOL_DESC = saved


def base_fams_for(text: str) -> list[str]:
    """Воспроизводит route-узел при включённой обрезке (react_loop.py ~2028-2031)."""
    routed = set(_route_families(text, k=len(_FAMILY_ROOTS)))
    return sorted((set(_LAZY_FAMILIES) - _PRUNABLE_FAMILIES) | (routed & _PRUNABLE_FAMILIES))


def main() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import sreda.db.models  # noqa: F401
    from sreda.db.base import Base

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    tid, uid = "tenant_probe", "user_probe"

    all_full = build(session, tid, uid, full=True)
    all_trim = build(session, tid, uid, full=False)
    before = tok(all_full)
    stage1 = tok(all_trim)

    print(f"инструментов всего: {len(all_trim)}")
    print(f"prunable семьи: {sorted(_PRUNABLE_FAMILIES)}")
    print(f"непрунабельные lazy (всегда привязаны): {sorted(set(_LAZY_FAMILIES) - _PRUNABLE_FAMILIES)}")
    print()
    print(f"BEFORE (полные описания, full-bind):  {before:6d} ток")
    print(f"STAGE1 (обрезка, full-bind):          {stage1:6d} ток   (−{100 * (before - stage1) // before}% от before)")

    floor_fams = sorted(set(_LAZY_FAMILIES) - _PRUNABLE_FAMILIES)
    floor = tok(_select_tools(all_trim, floor_fams))
    print(f"FLOOR  (core+{floor_fams}, ход без prunable-совпадения): {floor:6d} ток   (−{100 * (before - floor) // before}% от before)")

    rows = []
    for phrase in CORPUS:
        bf = base_fams_for(phrase)
        n = tok(_select_tools(all_trim, bf))
        rows.append((phrase, bf, n))
    vals = [n for *_, n in rows]
    p95 = sorted(vals)[min(len(vals) - 1, int(len(vals) * 0.95))]
    mean = statistics.mean(vals)
    print()
    print(f"PRUNED per-turn (Этап 2) на корпусе N={len(rows)}:")
    print(f"  токены/ход: min={min(vals)} p50={int(statistics.median(vals))} mean={int(mean)} p95={p95} max={max(vals)}")
    print(f"  средняя экономия: −{100 * (before - mean) / before:.0f}% от before; −{100 * (stage1 - mean) / stage1:.0f}% от stage1 (full-bind)")
    print("  --- по фразам (токены | привязанные prunable-семьи | фраза) ---")
    for phrase, bf, n in sorted(rows, key=lambda r: -r[2]):
        routed_prunable = [f for f in bf if f in _PRUNABLE_FAMILIES]
        print(f"   {n:5d}  [{','.join(routed_prunable) or '—'}]  «{phrase[:50]}»")


if __name__ == "__main__":
    main()
