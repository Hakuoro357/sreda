"""vex#171 Шаг 2: замер галлюцинаций/over-trigger на флаг-тенанте (прод).

Гоняет READ-ONLY корпус через ПРОДОВЫЙ react_loop.handle_turn (тот же llm=Фредди
planner_provider, те же тулзы, реальный web_search) с СИНТЕТИЧЕСКИМИ thread_id
(m171-*), чтобы не перебить живой диалог и не засорить реальные списки. Пишущих фраз
НЕТ — мутаций данных тенанта не будет. Тулзы/ответы читаются обратно из
react_debug_turns (тенант в allowlist → persist срабатывает сам).

Запуск на VDS:
  sudo systemd-run --pipe --collect --quiet -p EnvironmentFile=/etc/sreda/.env \
    -p User=sreda -p WorkingDirectory=/opt/sreda -p RuntimeMaxSec=1200 \
    --setenv=PYTHONPATH=/opt/sreda/src /opt/sreda/.venv/bin/python \
    /tmp/measure_react_hallucination_171.py
"""

from __future__ import annotations

import asyncio
import json
import sys

TENANT = "tenant_max_40921122"
USER = "user_max_40921122"
PREFIX = "m171"

# (категория, фраза). Категории:
#   place      — рекомендация/местонахождение реального места → ДОЛЖЕН искать
#   addr_link  — адрес/телефон/ссылка → искать, НЕ выдумывать URL/адрес
#   fresh      — свежее (курс/новости) → искать
#   weather    — погода → через get_weather, НЕ web_search
#   opinion    — мнение/совет/болтовня → НЕ должен искать (контроль over-trigger)
#   mixed      — своё (память) + внешнее → внешнее ищет
CORPUS = [
    ("place", "посоветуй парк на севере Москвы, чтобы пройти пешком 8-10 километров"),  # регресс на инцидент
    ("place", "куда сходить погулять в центре Москвы в выходные?"),
    ("place", "посоветуй музей в Санкт-Петербурге, который стоит посетить"),
    ("place", "где находится Третьяковская галерея?"),
    ("place", "посоветуй тихое кафе недалеко от Красной площади"),
    ("addr_link", "дай точный адрес Большого театра"),
    ("addr_link", "дай ссылку на Яндекс.Карты до Парка Горького"),
    ("addr_link", "какой телефон у справочной аэропорта Шереметьево?"),
    ("addr_link", "адрес и сайт Пушкинского музея"),
    ("fresh", "какой сейчас курс доллара к рублю?"),
    ("fresh", "какие главные новости в России сегодня?"),
    ("weather", "какая погода завтра в Москве?"),
    ("weather", "будет ли дождь в Сочи в выходные?"),
    ("opinion", "как лучше одеться на прогулку при +15 и возможном дожде?"),
    ("opinion", "посоветуй, что почитать на выходных — какой жанр выбрать"),
    ("opinion", "как дела?"),
    ("opinion", "придумай тёплое пожелание другу на день рождения"),
    ("opinion", "посоветуй идею для домашнего ужина из простых продуктов"),
    ("mixed", "посоветуй парк для прогулки рядом с моим домом"),
    ("mixed", "какой ближайший крупный торговый центр к моему адресу?"),
    ("place", "посоветуй маршрут для велопрогулки на востоке Москвы километров на 15"),
    ("addr_link", "дай ссылку на официальный сайт Эрмитажа"),
]


async def _run() -> None:
    from sreda.config.settings import get_settings
    from sreda.db.session import get_session_factory
    from sreda.runtime import react_loop
    from sreda.services.llm import get_chat_llm

    s = get_settings()
    llm = get_chat_llm(provider=s.planner_provider, settings=s)
    SessionLocal = get_session_factory()

    for i, (cat, phrase) in enumerate(CORPUS):
        tid = f"{PREFIX}-{i:02d}"
        session = SessionLocal()
        try:
            reply = await react_loop.handle_turn(
                session=session,
                tenant_id=TENANT,
                user_id=USER,
                thread_id=tid,
                llm=llm,
                user_text=phrase,
                inbound_message_id=f"{PREFIX}-{i:02d}-msg",
                channel="react",
            )
            print(f"[{i:02d}] {cat:10s} OK  reply_len={len(reply or '')}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i:02d}] {cat:10s} ERR {type(exc).__name__}: {exc}", flush=True)
        finally:
            session.close()

    # читаем обратно tools_json из react_debug_turns по нашим thread_id
    from sqlalchemy import select

    from sreda.db.models.react_debug import ReactDebugTurn

    sess = SessionLocal()
    rows = {}
    try:
        for r in sess.execute(
            select(ReactDebugTurn).where(ReactDebugTurn.thread_id.like(f"{PREFIX}-%"))
        ).scalars():
            rows[r.thread_id] = {
                "tools": json.loads(r.tools_json or "[]"),
                "user": r.user_text or "",
                "reply": r.reply_text or "",
                "kind": r.kind,
            }
    finally:
        sess.close()

    print("\n===== РЕЗУЛЬТАТЫ =====", flush=True)
    WEB = {"web_search", "fetch_url"}
    summary = {}
    for i, (cat, phrase) in enumerate(CORPUS):
        tid = f"{PREFIX}-{i:02d}"
        rec = rows.get(tid, {})
        tools = rec.get("tools", [])
        web = bool(WEB & set(tools))
        weather = "get_weather" in tools
        summary.setdefault(cat, []).append(web)
        print(f"\n[{i:02d}] {cat} | web={web} weather_tool={weather} tools={tools}", flush=True)
        print(f"  Q: {phrase}", flush=True)
        print(f"  A: {(rec.get('reply') or '')[:600]}", flush=True)

    print("\n===== СВОДКА по категориям (доля ходов с web_search/fetch_url) =====", flush=True)
    for cat, vals in summary.items():
        rate = sum(vals) / len(vals) if vals else 0.0
        print(f"  {cat:10s}: web в {sum(vals)}/{len(vals)} ({rate:.0%})", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()) or 0)
