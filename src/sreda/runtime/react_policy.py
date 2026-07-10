"""#285 Фаза A: TurnPolicy — per-turn политика инструментов (SHADOW-режим).

Единый путь ReAct (план chatfact-unify-final): один граф/промпт-путь + per-turn
детерминированная ToolPolicy. Фаза A — ТЕНЬ: объект строится из решений СУЩЕСТВУЮЩЕГО
сплита (#197 intent / #221 домены / #256 таймауты / #197 капы) и логируется side-by-side;
исполнением НЕ управляет (byte-identical — пин-тесты). Начнёт управлять в Фазе B
(двухъярусный write-гейт: allowed_write | confirm_only_write).

ИНВАРИАНТ SHADOW: этот модуль НИЧЕГО не мутирует — build_turn_policy чистая функция;
запись только в отдельный state-канал turn_policy_json (legacy-каналы router_allowed_*
не трогаются — контракт отката, plans/285-phase0-inventory.md §2 строка (б)).
"""

from __future__ import annotations

import json

# Пин web-only набора сплита (= react_preflight._WEB_ONLY_TOOL_NAMES; синхронность держит
# пин-тест — прямой импорт не нужен, набор стабилен с #197).
WEB_ONLY_TOOL_NAMES = ("web_search", "fetch_url", "get_weather")

POLICY_VERSION = 1


def build_turn_policy(
    *,
    intent: str | None,
    router_allowed_read: list[str] | None,
    router_allowed_write: list[str] | None,
    chat_timeout_sec: float,
    task_timeout_sec: float,
    chat_provider: str,
    search_caps: dict[str, int] | None,
) -> dict:
    """Выразить решения сплита ЯВНЫМ объектом полиси (shadow: структурирование, не выбор).

    intent: эффективный интент сплита (None = preflight OFF → легаси task-путь).
    router_allowed_*: legacy-каналы #221 (None = фильтр не применяется).
    search_caps: тугие капы сплита для этого интента ({tool: cap} | None вне chat/fact).
    Возвращает JSON-сериализуемый dict; без ПД (только режимы/домены/имена инструментов).
    """
    is_chatfact = intent in ("chat", "fact")
    return {
        "v": POLICY_VERSION,
        "source": "split-shadow",
        "intent": intent,
        "prompt_variant": "chat_fact" if is_chatfact else "task",
        # chat/fact: жёсткий web-only код-гейт сплита (_bind_for); task/OFF: доменные каналы #221
        # (None = без фильтра). В Фазе B здесь появятся ярусы (allowed_write | confirm_only_write).
        "web_scope_only": is_chatfact,
        "allowed_read_domains": (None if is_chatfact else router_allowed_read),
        "allowed_write_domains": ([] if is_chatfact else router_allowed_write),
        "provider_profile": {
            "variant": "chat_fact" if is_chatfact else "task",
            "provider_hint": chat_provider if is_chatfact else "planner",
            "timeout_sec": chat_timeout_sec if is_chatfact else task_timeout_sec,
        },
        "search_budget": dict(search_caps) if search_caps else None,
        # guard сплита: выключен на chat/fact (:2406/:2428), легаси-recovery на task
        "guard_scope": "off" if is_chatfact else "legacy",
    }


def dumps_policy(policy: dict) -> str:
    return json.dumps(policy, ensure_ascii=False, sort_keys=True)


# ─────────────────────────── Фаза B срез B2a: двухъярусная политика единого пути ───────────────────────────
# Чистая функция — соединяет детерминированные сигналы B1 (react_signals) с доменной онтологией #221
# (compute_allowed_domains) в per-turn политику. Пилляры 1/3 плана:
#   allowed_write (ярус а)  — ДЕТЕРМИНИРОВАННЫЙ write-грант БЕЗ confirm: командный сигнал B1 И домен
#                             резолвится #221 (иначе «поставь чайник» → ∅ → кандидат); + memory если
#                             декларативный stable-fact.
#   confirm_write (ярус б)  — на единый путь write НИКОГДА не deny-all: write-инструмент вне allowed_write
#                             биндится как КАНДИДАТ под универсальным confirm (B2b), не отказ. Всегда True
#                             на execute (unsignaled write → подтверждение, не тупик #281/#282).
#   allowed_read            — baseline web ВСЕГДА + own-data по read-кюсу B1 + домены записи (что пишем —
#                             читаем). НЕ включает route-домены сами по себе («как дела?»→checklists у #221,
#                             но read-кюс пуст → own-data не открывается — нейтрализация route-мины).
POLICY_VERSION_B2 = 2


def compute_unified_policy(text, route, classified=None, *, base_web=True,
                           sticky_memory_write=False,
                           prev_open_domains=frozenset()):
    """Двухъярусная политика единого пути (execute). Чистая; поведение прода не трогает (проводка — B2b).

    text: текст хода. route: RouteResult из route_domains(text). classified: DomainClassResult|None
    (LLM-фолбэк домена, как в #221). Возврат — dict (JSON-сериализуемый; без ПД: домены/флаги).

    sticky_memory_write (#319, «дверь открыта, пока ею пользуются»): ПРОШЛЫЙ ход реально записал в память
    → серия продолжается: memory в ярусе (а) БЕЗ confirm («бёдра 865» после «запиши крылья 615» не
    переспрашивает). Граница — по СМЫСЛУ, не по времени (Борис 2026-07-09): продлевается только фактом
    записи (renewal-by-use в run_tools), закрывается первым ходом без записи. PRE-CLEAR: явная команда в
    ДРУГОЙ раздел («добавь молоко в покупки») дверь НЕ продлевает и не пользуется ею — sticky не
    применяется, когда write-команда резолвится исключительно вне memory. Безопасно: в memory-семье нет
    деструктивных инструментов (save_core_fact/save_episode/create_memory_category)."""
    from sreda.runtime.react_preflight import compute_allowed_domains
    from sreda.runtime.react_signals import (
        declarative_memory_signal, read_cue_domains, write_command_signal,
    )

    w_sig = bool(write_command_signal(text))
    d_sig = bool(declarative_memory_signal(text))
    read_cues = set(read_cue_domains(text))

    allowed_write: set[str] = set()
    if w_sig:
        # ярус (а): домен(ы) резолвит #221 (single→write; compound/нет-домена→∅→кандидат). B1 гейтит
        # САМ факт команды (route.task_signal-мину для write игнорируем — пишем только при B1-сигнале).
        _ar, aw = compute_allowed_domains(route, classified)
        allowed_write |= set(aw)
        # memory-ИДИОМА (B2 субагент MAJOR): «сохрани в тайне»/«запиши в дневник» → write_cmd=True И
        # route→memory (корни памяти сохран/запиш) → был бы ПРЯМОЙ memory-write в обход confirm.
        # Контракт B1↔B2 «нет домена → кандидат» здесь не спасал (домен ЕСТЬ). Прямой memory-грант —
        # ТОЛЬКО по декларативу; командные memory-writes («запомни X», «сохрани рецепт») → ярус (б) confirm.
        allowed_write.discard("memory")
    if d_sig:
        allowed_write.add("memory")

    # #319 sticky-by-use: прошлый ход записал в память → memory в ярус (а) на ЭТОТ ход, КРОМЕ pre-clear:
    # ЛЮБАЯ write-команда, НЕ резолвящаяся в memory («добавь молоко в покупки» → shopping; R2 medium
    # MAJOR: и БЕЗ-доменная «поставь чайник» — иначе sticky ломал бы контракт «нет домена → кандидат»).
    # Продолжают серию: голые данные («бёдра 865», w_sig=False) и «запиши спинки 900» (route→memory).
    # NB (R1 субагент MINOR, осознанно): мид-серии «запиши X» пишет БЕЗ confirm — идиома-гейт memory
    # (см. discard выше) перекрывается sticky by design (серия уже подтверждена фактом первой записи).
    # applied-флаг — в signals (наблюдаемость канарейки).
    sticky_applied = False
    if sticky_memory_write:
        _non_memory_cmd = bool(w_sig and "memory" not in route.all_domains)
        if not _non_memory_cmd:
            allowed_write.add("memory")
            sticky_applied = True

    # R5-переделка (владелец: «не костылями»): ЕДИНЫЙ гейт продолжения - сообщение
    # юзера НЕ несёт самостоятельной темы: ни доменных слов (route: «покупки»,
    # «задачи», «напоминания»...), ни read-кюсов. Тогда «В 15» / «завтра в 9» /
    # «Поставь» / «да» = продолжение открытого хода; ЛЮБОЕ доменное слово
    # («перескажи напоминания», «что с задачами», «добавь молоко в покупки»,
    # и даже «напоминание в 15» - осознанный residual: лишний ЧЕЛОВЕЧЕСКИЙ confirm)
    # = обычный вход со страховкой. Ошибка строго в безопасную сторону. Никаких
    # списков фраз: только существующие детекторы route_domains/read_cue_domains.
    continuation: list = []
    if not route.all_domains and not read_cues:
        for _d in sorted(prev_open_domains):
            allowed_write.add(_d)
            continuation.append(_d)

    allowed_read: set[str] = set()
    if base_web:
        allowed_read.add("web")
    allowed_read |= read_cues
    allowed_read |= allowed_write  # что разрешено писать — разрешено и читать (найти объект правки)

    return {
        "v": POLICY_VERSION_B2,
        "mode": "unified-execute",
        "allowed_read": sorted(allowed_read),
        "allowed_write": sorted(allowed_write),   # ярус (а): прямой write без confirm
        "confirm_write": True,                    # ярус (б): write вне allowed_write → кандидат+confirm (B2b)
        "signals": {"write_cmd": w_sig, "declarative": d_sig, "read_cues": sorted(read_cues),
                    "sticky_memory": sticky_applied,
                    "turn_continuation": continuation},  # #338: наблюдаемость наследования
    }
