#!/usr/bin/env python
"""#165 Этап 2 — замер МЕХАНИЗМА выбора семей инструментов на ход.
Сравнивает 3 роутера на одном размеченном корпусе: латентность + точность (ожидаемая
семья ∈ top-k), k=1/2/3.

  A. embeddings — 9 описаний семей → векторы (BGE-m3); фраза → embed_query → cosine → top-k.
  B. keyword    — словарь корней слов по семьям; подсчёт совпадений → top-k. БЕЗ сети.
  C. freddie    — дешёвый вызов Фредди «какие семьи нужны».

Запуск на VDS (там и embeddings-ключ, и Фредди-провайдер), из temp-worktree, прод не трогаем:
  systemd-run -p EnvironmentFile=/etc/sreda/.env -p User=sreda -p WorkingDirectory=<wt> \
    --setenv=PYTHONPATH=<wt>/src /opt/sreda/.venv/bin/python scripts/measure_family_router.py
"""
from __future__ import annotations

import asyncio
import math
import statistics
import time

from langchain_core.messages import HumanMessage, SystemMessage

from sreda.config.settings import get_settings
from sreda.services.embeddings import get_embeddings_client
from sreda.services.llm import get_chat_llm

# Короткие описания семей — эталон для эмбеддингов И список для Фредди-роутера.
FAMILY_DESC: dict[str, str] = {
    "reminders": "Напоминания на время: создать, показать, изменить, удалить напоминание; разбудить, не забыть к часу.",
    "tasks": "Задачи и дела с датой/временем: добавить дело, показать задачи, выполнить, отменить, перенести, запланировать на день.",
    "shopping": "Список покупок: добавить/убрать товары, отметить купленным, корзина, магазин, продукты.",
    "recipes": "Книга рецептов: сохранить рецепт, найти свой рецепт, удалить; ингредиенты, приготовление блюда.",
    "menu": "Меню на неделю: спланировать меню, показать/изменить, собрать список покупок из меню; завтрак, обед, ужин.",
    "household": "Члены семьи: добавить/показать/изменить/удалить родных, состав семьи, жена, муж, дети.",
    "checklists": "Чек-листы — списки дел без даты: создать список, добавить/отметить/удалить пункты, план дел, сборы.",
    "memory": "Память о пользователе: запомнить факт о себе, вспомнить что рассказывал раньше, что я говорил про.",
    "web": "Внешний мир: погода, поиск в интернете, новости, скачать веб-страницу.",
}
FAMILIES = list(FAMILY_DESC)

# Словарь корней (substring по lowercase; крудая русская морфология). ЧЕРНОВИК — на проде
# собирать из реальных трасс. Намеренно есть пересечения (дела↔задачи/чеклисты) — замер покажет.
KEYWORD_DICT: dict[str, list[str]] = {
    "reminders": ["напомн", "разбуд", "будильник", "не забыть", "не дай забыть"],
    "tasks": ["задач", "запланир", "сделать к", "дело на", "дела на", "успеть", "дедлайн"],
    "shopping": ["купи", "куплю", "купить", "магазин", "корзин", "продукт", "список покупок", "в магаз"],
    "recipes": ["рецепт", "приготов", "блюд", "ингредиент", "как готовить"],
    "menu": ["меню", "на неделю", "ужин", "обед", "завтрак", "поужина", "что приготовить на"],
    "household": ["семь", "жена", "муж", "дети", "ребён", "ребен", "родн", "член семьи", "дочь", "сын"],
    "checklists": ["чек-лист", "чеклист", "список дел", "пункт", "сборы", "что взять", "не забыть взять"],
    "memory": ["запомни", "помнишь", "вспомни", "не забудь что я", "факт обо мне", "я говорил", "я рассказывал"],
    "web": ["погод", "найди в интернет", "поищи", "новости", "курс ", "сколько стоит", "загугли"],
}

# (фраза, (ожидаемые семьи)) — первая = основная. ~45 фраз; есть смешанные и ловушки.
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
    ("запиши в дела по машине: масло, фильтр, шиномонтаж", ("checklists",)),
    ("покажи мой список дел по ремонту", ("checklists",)),
    ("отметь пункт масло выполненным", ("checklists",)),
    ("собери список что взять в поход", ("checklists",)),
    ("запомни что я не ем глютен", ("memory",)),
    ("что я тебе рассказывал про мою работу", ("memory",)),
    ("не забудь что я аллергик на орехи", ("memory",)),
    ("какая погода завтра в москве", ("web",)),
    ("поищи в интернете новости про курс рубля", ("web",)),
    ("сколько стоит билет на поезд до питера", ("web",)),
    # смешанные домены (ожидаем обе)
    ("добавь рецепт и сразу запланируй его на ужин в пятницу", ("recipes", "menu")),
    ("собери список покупок из моего меню", ("menu", "shopping")),
    ("отмени задачу про уборку и убери её в чек-лист", ("tasks", "checklists")),
    ("напомни купить подарок и добавь его в список покупок", ("reminders", "shopping")),
    # болтовня / вне scope (ожидаем НИ ОДНОЙ)
    ("спасибо, ты супер", ()),
    ("привет, как дела", ()),
    ("оплати за меня счёт за свет", ()),
]


def _cosine(a: list[float], b: list[float]) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


def route_keyword(phrase: str) -> list[str]:
    low = phrase.lower()
    scored = [(fam, sum(1 for kw in kws if kw in low)) for fam, kws in KEYWORD_DICT.items()]
    scored = [(f, n) for f, n in scored if n > 0]
    scored.sort(key=lambda x: -x[1])
    return [f for f, _ in scored]


def route_embed(phrase: str, fam_vecs: dict[str, list[float]], client) -> tuple[list[float], list[str]]:
    v = client.embed_query(phrase)
    ranked = sorted(FAMILIES, key=lambda f: -_cosine(v, fam_vecs[f]))
    return v, ranked


_FREDDIE_SYS = (
    "Ты — маршрутизатор тем. По сообщению пользователя выбери от 0 до 2 тем из списка, "
    "к которым оно относится. Ответ — ТОЛЬКО названия через запятую, без пояснений. "
    "Если болтовня или вне списка — ответь: none.\n"
    "Темы: reminders (напоминания), tasks (задачи/дела с датой), shopping (покупки), "
    "recipes (рецепты), menu (меню недели), household (семья), checklists (списки дел), "
    "memory (память о пользователе), web (погода/интернет)."
)


async def route_freddie(phrase: str, llm) -> list[str]:
    resp = await llm.ainvoke([SystemMessage(_FREDDIE_SYS), HumanMessage(phrase)])
    txt = (getattr(resp, "content", "") or "").lower()
    return [f for f in FAMILIES if f in txt]


def _acc(rows, k: int) -> int:
    # попадание = основная ожидаемая семья в top-k (для пустого ожидания — пусто ли в top-k)
    hit = 0
    for _phrase, exp, picked in rows:
        topk = picked[:k]
        if not exp:
            if not topk:
                hit += 1
        elif exp[0] in topk:
            hit += 1
    return hit


def _multi_cov(rows, k: int) -> tuple[int, int]:
    multi = [(e, p) for _ph, e, p in rows if len(e) >= 2]
    cov = sum(1 for e, p in multi if all(x in p[:k] for x in e))
    return cov, len(multi)


def _report(label: str, rows, lats):
    n = len(rows)
    print(f"\n===== {label} =====")
    if lats:
        p50 = statistics.median(lats)
        p95 = sorted(lats)[min(len(lats) - 1, int(len(lats) * 0.95))]
        print(f"латентность роутера: p50={p50*1000:.0f}мс p95={p95*1000:.0f}мс")
    else:
        print("латентность роутера: ~0 (без сети)")
    for k in (1, 2, 3):
        h = _acc(rows, k)
        cov, m = _multi_cov(rows, k)
        print(f"  top-{k}: точность {h}/{n} ({100*h//n}%) | смешанные обе-покрыты {cov}/{m}")
    print("  --- промахи (top-2) ---")
    for ph, exp, picked in rows:
        if (not exp and picked[:2]) or (exp and exp[0] not in picked[:2]):
            print(f"    «{ph[:44]}» ожид={exp or '∅'} top2={picked[:2]}")


async def main():
    settings = get_settings()
    client = get_embeddings_client()
    print(f"embeddings client: {type(client).__name__}; planner={settings.planner_provider}")

    # эталонные векторы семей (один раз)
    t0 = time.monotonic()
    fam_vecs = {f: client.embed_document(FAMILY_DESC[f]) for f in FAMILIES}
    print(f"эталон 9 семей построен за {(time.monotonic()-t0)*1000:.0f}мс (разово)")

    # A embeddings
    emb_rows, emb_lats = [], []
    for ph, exp in CORPUS:
        t = time.monotonic()
        _v, ranked = route_embed(ph, fam_vecs, client)
        emb_lats.append(time.monotonic() - t)
        emb_rows.append((ph, exp, ranked))
    _report("A. EMBEDDINGS (BGE-m3)", emb_rows, emb_lats)

    # B keyword
    kw_rows = [(ph, exp, route_keyword(ph)) for ph, exp in CORPUS]
    _report("B. KEYWORD (словарь, без сети)", kw_rows, None)

    # C freddie
    llm = get_chat_llm(provider=settings.planner_provider, settings=settings)
    fr_rows, fr_lats = [], []
    for ph, exp in CORPUS:
        t = time.monotonic()
        try:
            picked = await route_freddie(ph, llm)
        except Exception as e:  # noqa: BLE001
            picked = []
            print(f"  freddie err {type(e).__name__} на «{ph[:30]}»")
        fr_lats.append(time.monotonic() - t)
        fr_rows.append((ph, exp, picked))
    _report("C. FREDDIE-роутер (вызов LLM)", fr_rows, fr_lats)


if __name__ == "__main__":
    asyncio.run(main())
