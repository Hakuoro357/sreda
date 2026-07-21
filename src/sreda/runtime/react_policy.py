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
                           prev_open_domains=frozenset(),
                           disambiguator=None,
                           read_intent_domains=None):
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
        declarative_memory_signal, read_cue_domains, read_cue_groups, write_command_signal,
    )

    w_sig = bool(write_command_signal(text))
    d_sig = bool(declarative_memory_signal(text))
    read_cues = set(read_cue_domains(text))

    allowed_write: set[str] = set()
    if w_sig:
        # ярус (а): домен(ы) резолвит #221 (single→write; compound/нет-домена→∅→кандидат). B1 гейтит
        # САМ факт команды (route.task_signal-мину для write игнорируем — пишем только при B1-сигнале).
        # #352: classified сюда НЕ передаём — LLM-домен на едином пути НИКОГДА не открывает write
        # (легаси-контракт «high → read+write» остаётся на легаси-пути #221); запись по LLM-домену
        # идёт кандидатом под человеческий confirm (ярус б).
        _ar, aw = compute_allowed_domains(route, None)
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
    if not route.all_domains and not read_cues and not sticky_applied:
        # R7 оба Codex: sticky-серия (memory) и открытый слот НЕ складываются -
        # два прямых write-домена на themeless-ответе неоднозначны → fail-closed:
        # активная sticky-серия побеждает (юзер в memory-контексте), слот идёт
        # через человеческий кандидат-confirm.
        for _d in sorted(prev_open_domains):
            allowed_write.add(_d)
            continuation.append(_d)

    allowed_read: set[str] = set()
    if base_web:
        allowed_read.add("web")
    allowed_read |= read_cues
    allowed_read |= allowed_write  # что разрешено писать — разрешено и читать (найти объект правки)

    # #399 ШОВ read-намерения. Проблема: read-кюсы — щедрый, но НЕ полный детектор запроса
    # чтения («покажи спис-О-к кино» мимо `\bсписк` из-за беглой гласной) → ложный отказ
    # «не могу показать» (0/15 на проде). Провести route-раздел напрямую НЕЛЬЗЯ: «как дела»
    # роутится в checklists и открыло бы own-data на болтовне (мина #285).
    #
    # Поэтому read_allowed = read_cues ИЛИ read_intent_domains — ДИЗЪЮНКЦИЯ ИСТОЧНИКОВ, и
    # параметр СПЕЦИАЛЬНО доменный (не «чек-листовый флаг»): сегодня его наполняет
    # детерминированный детектор #213 (вариант A, русский, только checklists), завтра —
    # read/write-бит от LLM (вариант B, языко-нейтральный, любой раздел) вливается в ТОТ ЖЕ
    # аргумент объединением, без переписывания проводки. Это и есть шов под будущий B.
    #
    # ГРАНИЦА БЕЗОПАСНОСТИ (дисциплина subtract-only #376): сигнал только ПОДТВЕРЖДАЕТ раздел,
    # уже поднятый ДЕТЕРМИНИРОВАННЫМ роутером — пересечение с route.all_domains. Ввести НОВЫЙ
    # раздел он не может: иначе недоверенный источник (LLM на истории с инъекцией) авторизовал
    # бы чтение чужого раздела. Write не трогается ни при каких условиях (чтение ≠ мутация).
    # `None` = источник не подан (флаг OFF) → ветка мертва, форма политики и трейса
    # байт-в-байт прежняя. Пустой frozenset = флаг ON, сигнал промолчал — это РАЗНЫЕ
    # состояния: второе обязано быть видно в трейсе (замер остаточных промахов).
    read_intent = set(read_intent_domains or ()) & set(route.all_domains)
    allowed_read |= read_intent

    # #352: LLM-фолбэк домена (classify_domains, владелец 2026-07-11 «нахера выкидываешь результат
    # ллм») — ТОЛЬКО чтение/загрузка раздела, write не расширяет (мутация по LLM-домену = кандидат
    # под confirm, ярус б). Применяется лишь high (ровно один домен, строгий enum-парс); low
    # (мусор/несколько/сбой) игнорируется — политика как без классификатора (fail-safe).
    llm_domains: list = []
    if classified is not None and classified.confidence == "high":
        llm_domains = sorted(classified.domains)
        allowed_read |= set(llm_domains)

    # #376: subtract-only дизамбигуация неоднозначной read-кюс-группы. disambiguator (умный
    # классификатор ЭТОГО хода, ОТДЕЛЬНО от continuation-classified выше) high с ровно 1 доменом →
    # ВЫЧИТАЕТ остальных членов неоднозначной read-кюс-группы из allowed_read (на «список кино»
    # убирает shopping, оставляя checklists). ТОЛЬКО вычитание: домен ВНЕ поднятых детерминированно
    # (route.all_domains ∪ read_cues) — это add (напр. промпт-инъекция из истории), НЕ применяем;
    # вызывающий фиксирует как divergence. write НЕ трогаем (allowed_write вычтен из subtract);
    # compound/cross пропускаем целиком (reminders/menu не теряются). Гарантия «shopping вырезан»
    # держится на allowed_read → router_allowed_read_domains (фильтр _apply_unified_policy),
    # устойчива к freshness/need_family re-add (те трогают active_families, не router_allowed_read).
    disambig_kind = None
    disambig_domains: list = []
    if (disambiguator is not None and disambiguator.confidence == "high"
            and len(disambiguator.domains) == 1
            and not route.compound_by_connector and route.cross_intent is None):
        disambig_domains = sorted(disambiguator.domains)
        _fd = set(disambiguator.domains)
        _raised = set(route.all_domains) | read_cues
        if _fd <= _raised:
            disambig_kind = "subtract"
            # CR R1 sol MAJOR: домен, поднятый ДРУГОЙ кюс-группой, вычитать нельзя —
            # «список кино и покупки» даёт {checklists,shopping} И {shopping}: вердикт
            # checklists не должен снять shopping, явно затребованный второй группой.
            _groups = [set(_grp) - {"web"} for _grp in read_cue_groups(text)]
            for _i, _g in enumerate(_groups):
                if len(_g) > 1 and (_fd & _g):
                    _others: set = set().union(
                        *(_groups[:_i] + _groups[_i + 1:])) if len(_groups) > 1 else set()
                    allowed_read -= (_g - _fd) - allowed_write - _others
        else:
            disambig_kind = "add"  # анти-инъекция: НЕ применяем, только divergence снаружи

    _signals = {"write_cmd": w_sig, "declarative": d_sig, "read_cues": sorted(read_cues),
                "sticky_memory": sticky_applied,
                "turn_continuation": continuation,   # #338: наблюдаемость наследования
                "llm_domains": llm_domains}          # #352: что дал LLM-классификатор
    if disambiguator is not None:
        # CR R1 sol/terra MAJOR: без дизамбигуатора форма signals ПРЕЖНЯЯ байт-в-байт
        # (поля не добавляются) — флаг OFF не меняет ни политику, ни трейс.
        _signals["disambig_kind"] = disambig_kind    # #376: subtract | add | None
        _signals["disambig_domains"] = disambig_domains
    if read_intent_domains is not None:
        # #399: та же дисциплина — поле появляется ТОЛЬКО под своим флагом.
        # CR R1 sol MINOR: пишем то, что РЕАЛЬНО осталось открыто (пересечение с итоговым
        # allowed_read) — #376-subtract ниже мог вычесть раздел, и поле «signal сказал
        # checklists» противоречило бы фактической политике.
        _signals["read_intent"] = sorted(read_intent & allowed_read)

    return {
        "v": POLICY_VERSION_B2,
        "mode": "unified-execute",
        "allowed_read": sorted(allowed_read),
        "allowed_write": sorted(allowed_write),   # ярус (а): прямой write без confirm
        "confirm_write": True,                    # ярус (б): write вне allowed_write → кандидат+confirm (B2b)
        "signals": _signals,
    }


def read_intent_residual_miss(text, route, policy) -> bool:
    """#399: ОСТАТОЧНЫЙ ПРОМАХ — роутер поднял checklists, юзер ЯВНО просит показать,
    а чтение так и не открылось. Ровно тот хвост ложных отказов, который фикс мог бы
    замаскировать: явный баг закрылся, а непойманные детектором формулировки стали бы
    невидимы. Замер — обязательная часть поставки #399, не «потом».

    CR R1 sol MINOR: формула живёт ЗДЕСЬ (продакшн), а не дублируется в тесте — иначе
    дрейф проводки в react_loop остался бы зелёным. Чистая функция: и ход, и тест
    считают ОДНО И ТО ЖЕ.

    Болтовня сюда не попадает: «как дела» роутится в checklists, но запросом чтения не
    является (`is_read_request`) → не промах, а корректный отказ. Утверждения/цитаты —
    тоже не промах."""
    from sreda.runtime.react_signals import is_read_request
    return bool("checklists" in set(route.all_domains)
                and "checklists" not in set(policy.get("allowed_read") or ())
                and is_read_request(text or ""))
