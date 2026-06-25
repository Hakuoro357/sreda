"""#197 — Preflight intent-router для ReAct-цикла.

ДО цикла классифицируем намерение и роутим:
- task → Фредди + полный набор инструментов (как сейчас);
- chat/fact → рассуждающая модель (deepseek) + ТОЛЬКО web-семья + поиск≤1 (анти-флейл, инцидент 2026-06-23).

Слои (см. plans/197-preflight-final.md):
- Слой 0 `_must_task` — узкий ДЕТЕРМИНИРОВАННЫЙ high-precision override (явные productivity-команды → task
  без LLM). Защищает task-контракт от ошибки классификатора. НЕ голые местоимения (negative-кейсы).
- Слой 1 `classify_intent` — СОВЕТУЮЩИЙ LLM-классификатор на Фредди (async, строгий парс, fail-open task).

Безопасность (инвариант #197): SCOPE всегда по интенту (chat/fact → web-only ВСЕГДА); fail-open в task —
ТОЛЬКО на этапе ОПРЕДЕЛЕНИЯ интента (здесь), не после.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Iterable, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import logging

logger = logging.getLogger(__name__)

Intent = Literal["task", "chat", "fact"]
_VALID_INTENTS = frozenset({"task", "chat", "fact"})

# Единый источник истины: инструменты web-семьи, доступные на chat/fact-пути (mimo R4: общая
# константа, не литералы в _bind_for). Синхронно с семьёй "web" (web_search/fetch_url/get_weather).
_WEB_ONLY_TOOL_NAMES = frozenset({"web_search", "fetch_url", "get_weather"})

# Слой 0: HIGH-PRECISION шаблоны явных productivity-команд/own-data → task БЕЗ классификатора.
# Подстрочный матч по нормализованному (lower, дефисы убраны) тексту. ВАЖНО (Codex R3/R4): НЕ голые
# местоимения «я/мне/мой» — иначе «расскажи мне про Пушкина» ошибочно станет task (negative-кейс).
# False-positive здесь = чат уходит в task (теряет deepseek + ВОЗВРАТ flail-поверхности) → держим узко;
# false-negative безопасен (доедет до классификатора). Калибруется test_must_task_high_precision.
_MUST_TASK_PATTERNS: tuple[str, ...] = (
    # напоминания
    "напомни", "напоминани",
    # задачи (явные глаголы/обороты)
    "поставь задач", "добавь задач", "заведи задач", "новая задача", "новую задачу",
    "мои задачи", "мои дела", "покажи задач", "покажи дела", "список задач", "список дел",
    "отмени задач", "заверши задач", "выполни задач", "удали задач", "заверши дело",
    # собственные данные / расписание пользователя
    "что у меня", "какие у меня", "что мне сегодня", "что мне завтра", "что мне нужно сделать",
    # покупки/списки (явные обороты, НЕ голое «купить»)
    "добавь в список", "в список покупок", "список покупок", "мой список", "в покупки",
    "удали из списка", "вычеркни из списка",
    # перенос/изменение собственных дел (требуем предлог-контекст «на», чтобы сузить)
    "перенеси на", "перенеси задач", "перенеси напомин", "отмени напомин", "удали напомин",
    # память/заметки
    "запомни", "запиши, что", "запиши что", "сделай заметку", "в памяти",
)

def _normalize(text: str) -> str:
    return re.sub(r"[-‐‑‒–—]", "", (text or "").lower())


def _must_task(text: str, prev_intent: str | None = None) -> bool:
    """Слой 0: ТОЛЬКО явная productivity-команда/own-data → task (high-precision, чисто лексический).
    Нет матча → False (пусть решает классификатор). NEG: «расскажи/найди мне про X» → False.

    `prev_intent` — НЕ используется здесь (code-review R1: эллиптический «короткий follow-up → task»
    был слишком широк — «да/нет/спасибо/кто Пушкин?» после task-хода ошибочно уходили в task+полный
    набор, мимо классификатора). Follow-up'ы теперь решает классификатор, которому prev_intent
    передаётся МЯГКОЙ подсказкой (см. classify_intent). Параметр сохранён для стабильности сигнатуры."""
    norm = _normalize(text)
    return any(pat in norm for pat in _MUST_TASK_PATTERNS)


def _parse_intent(raw: Any) -> Intent:
    """Строгий парс вывода классификатора: первое слово lower ∈ {task,chat,fact}; иначе fail-open task."""
    s = "" if raw is None else str(raw)
    m = re.search(r"[a-zа-яё]+", s.lower())
    word = m.group(0) if m else ""
    return word if word in _VALID_INTENTS else "task"  # type: ignore[return-value]


_CLASSIFIER_SYSTEM = (
    "Ты — классификатор намерения сообщения пользователю-ассистенту. Верни РОВНО ОДНО слово на "
    "латинице: task, chat или fact. Без пояснений.\n"
    "- task: пользователь хочет ЧТО-ТО СДЕЛАТЬ или обращается к СВОИМ данным — напоминания, задачи, "
    "списки, покупки, меню, рецепты, члены семьи, заметки/память; обороты «мои/у меня/напомни/добавь/"
    "удали/перенеси/запомни».\n"
    "- fact: вопрос на ОБЩЕЕ (публичное) знание о мире — «кто/что/когда/сколько», НЕ про данные пользователя.\n"
    "- chat: болтовня, игра, викторина, смолток, мнение, приветствие.\n"
    "Если сомневаешься между task и не-task — выбирай task."
)


def _format_recent(messages: Iterable[Any], limit: int = 4) -> str:
    """Короткая выжимка последних реплик (роль: текст) для контекста классификатора."""
    rows: list[str] = []
    for m in list(messages or [])[-limit:]:
        role = "user" if isinstance(m, HumanMessage) else ("assistant" if isinstance(m, AIMessage) else "")
        if not role:
            continue
        content = m.content if isinstance(m.content, str) else str(m.content or "")
        content = content.strip().replace("\n", " ")
        if content:
            rows.append(f"{role}: {content[:300]}")
    return "\n".join(rows)


async def classify_intent(
    recent_messages: Iterable[Any], user_text: str, prev_intent: str | None,
    freddie_llm: Any, timeout: float = 4.0, raw_sink: list[str] | None = None,
) -> Intent:
    """Слой 1: советующий LLM-классификатор на Фредди. Async, строгий парс, fail-open task при
    любом сбое/таймауте/мусоре. Вход: последние реплики + текущее сообщение + прошлый интент (мягко).
    `raw_sink` (опц.) — если передан, в него кладётся сырой ответ модели (для трейса classifier_raw, #192)."""
    hint = (f"Прошлый интент: {prev_intent}. Интент МОЖЕТ смениться — если сообщение явно про задачу, "
            "ставь task." if prev_intent in _VALID_INTENTS else "")
    recent = _format_recent(recent_messages)
    payload = (f"{hint}\n\nПоследние реплики:\n{recent}\n\nТекущее сообщение пользователя:\n{user_text}\n\n"
               "Одно слово (task/chat/fact):")
    try:
        resp = await asyncio.wait_for(
            freddie_llm.ainvoke([SystemMessage(content=_CLASSIFIER_SYSTEM),
                                 HumanMessage(content=payload)]),
            timeout=timeout)
        raw = getattr(resp, "content", resp)
        if raw_sink is not None:
            raw_sink.append(str(raw)[:120])  # сырой вывод классификатора для трейса (одно слово)
        intent = _parse_intent(raw)
        logger.info("react_preflight: classify raw=%r → %s", str(raw)[:60], intent)
        return intent
    except Exception:  # noqa: BLE001 — таймаут/сеть/провайдер → fail-open task (этап определения интента)
        logger.warning("react_preflight: classify_intent failed → fail-open task", exc_info=True)
        return "task"


# ───────────────────────── #215: детерминированная карта «слово → раздел» ─────────────────────────
# Эталон ontology (lock 2026-06-24): «дела» = СПИСКИ ДЕЛ (чек-листы), «задачи» = tasks_items,
# «напоминания» = алерты. Фредди (быстрая модель) сам путал «покажи дела» → list_reminders. Урок #180:
# промпт быстрой модели не держит → ловим слово-раздел ДЕТЕРМИНИРОВАННО и вставляем жёсткую директиву.
# Матч по ГРАНИЦЕ слова (токены), не подстрокой — иначе «Сделай»/«делать» ложно матчат «дела».
_WORD_RE2 = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
_SEC_REMINDER_ROOTS = ("напомн", "напомин")        # напомни/напомнить (напомн-) + напоминание (напомин-)
_SEC_TASK_ROOTS = ("задач", "расписани")           # задача/задачи/расписание
_SEC_CHECKLIST_ROOTS = ("списк", "чеклист")         # списки/списка/списком/чеклист (дефис нормализован)
# «список» (ед.ч.) = «спис-О-к» → НЕ начинается с «списк»; добавляем формы ед.числа явными словами.
_SEC_CHECKLIST_WORDS = frozenset({"дела", "дело", "делах", "делам", "делами",
                                  "список", "списке", "списку", "списком"})

_HINT_REMINDER = ("ВАЖНО: пользователь спрашивает про НАПОМИНАНИЯ (сигнал в конкретное время). "
                  "Используй list_reminders / schedule_reminder. НЕ показывай задачи и не показывай списки дел.")
_HINT_TASK = ("ВАЖНО: пользователь спрашивает про ЗАДАЧИ (раздел «Задачи» — дела с датой/временем, "
              "напр. «позвонить маме»). Используй list_tasks / add_task. НЕ показывай напоминания и чек-листы.")
_HINT_CHECKLIST = ("ВАЖНО: пользователь спрашивает про СПИСКИ ДЕЛ (раздел «Дела» = чек-листы со списками "
                   "пунктов, напр. «Кино к просмотру», «Поход», покупки). Используй list_checklists. "
                   "НЕ показывай напоминания (list_reminders) и НЕ показывай задачи (list_tasks).")


def _section_hint(text: str) -> str | None:
    """#215: детерминированно по слову-разделу → жёсткая директива для chat-узла (какой list_* звать).
    Приоритет: напоминания → задачи → списки дел (от специфичного к широкому). Нет слова → None."""
    toks = _WORD_RE2.findall(re.sub(r"[-‐‑‒–—]", "", (text or "").lower()))

    def has_root(roots: tuple[str, ...]) -> bool:
        return any(t.startswith(r) for t in toks for r in roots)

    if has_root(_SEC_REMINDER_ROOTS):
        return _HINT_REMINDER
    if has_root(_SEC_TASK_ROOTS):
        return _HINT_TASK
    if has_root(_SEC_CHECKLIST_ROOTS) or any(t in _SEC_CHECKLIST_WORDS for t in toks):
        return _HINT_CHECKLIST
    return None


# ───────────────────────── #221 Ф1: единый ontology-роутер доменов ─────────────────────────
# Слияние ТРЁХ словарей (_MUST_TASK #197 / _SEC_* #215 / _FAMILY_ROOTS #165) в ОДИН источник «слово→
# раздел(ы)». РЕФРЕЙМ (план v5): intent (task/chat/fact) остаётся авторитетом #197; домены — надстройка
# на task-пути (какие семьи привязать). R1-ревью (Codex high/medium + субагент):
# - intent_hint=task ТОЛЬКО от task-сигнала (#197 _must_task / #215 _section_hint / intent_only), НЕ от
#   семейных корней (иначе «найди про X»/«погода» ложно → task → потеря deepseek, инцидент 2026-06-23);
# - compound ТОЛЬКО при союзе МЕЖДУ клаузами (split), не от любого союза в тексте;
# - longest-match через фразы; данные семей — из нейтрального react_routing_data (нет импорта react_loop).
from dataclasses import dataclass
from functools import lru_cache

from sreda.runtime.react_routing_data import FAMILY_EXACT_ROOTS, FAMILY_ROOTS, LAZY_FAMILIES

# Приоритет тай-брейка: action-домены (reminders/tasks/memory — глагол-действие) ВЫШЕ content
# (shopping/recipes/menu/household), затем web, checklists ПОСЛЕДНИЙ («списк/дела» самые общие).
# R2-ревью: memory выше content («запомни рецепт борща»→memory, не recipes); recipes выше menu
# («рецепт на ужин»→recipes, не menu).
_DOMAIN_PRIORITY: tuple[str, ...] = (
    "reminders", "tasks", "memory", "shopping", "recipes", "menu", "household", "web", "checklists")
_CONNECTORS_RE = re.compile(r"\b(?:и|потом|затем|также|плюс)\b")  # split на клаузы; compound только МЕЖДУ ними
# Многословные фразы (longest-match): форсируют домен поверх одиночных корней — разрешают «список X».
_PHRASES: tuple[tuple[str, str], ...] = (
    ("список покупок", "shopping"), ("списка покупок", "shopping"), ("списке покупок", "shopping"),
    ("список покупк", "shopping"), ("список дел", "checklists"), ("списки дел", "checklists"),
    ("список задач", "tasks"))
# «в памяти» exact-словами (НЕ префикс «памят» — он ловил бы «памятник»/«памятный», R1 negative).
_MEMORY_WORDS = frozenset({"памяти", "память", "памятью"})
_DIRECTIVE = {"reminders": _HINT_REMINDER, "tasks": _HINT_TASK, "checklists": _HINT_CHECKLIST}
# Action-домены (императив-глагол = намерение действия): выбираются в клаузе ПЕРВЫМИ, поверх фразы и
# content (R3 Codex high: «запомни список покупок»→memory, «напомни список покупок»→reminders — глагол
# важнее фразы «список покупок»→shopping).
_ACTION_DOMAINS = frozenset({"reminders", "tasks", "memory"})


@dataclass(frozen=True)
class RouteResult:
    primary_domain: str | None
    secondary_domains: tuple[str, ...]
    suppressed_domains: tuple[str, ...]
    compound_by_connector: bool
    intent_hint: str | None              # "task" ТОЛЬКО от task-сигнала #197/#215, иначе None
    intent_only: bool                    # task-сигнал БЕЗ конкретного раздела
    active_families: tuple[str, ...]     # домены ∩ ленивые (БЕЗ core reminders/tasks — предзагрузка)
    directive: str | None
    all_domains: tuple[str, ...] = ()    # primary ∪ secondary — для Ф3 split/confirm (НЕ active_families)


@lru_cache(maxsize=1)
def _ontology() -> dict:
    """Единая карта из НЕЙТРАЛЬНЫХ данных (react_routing_data) + _SEC_* (#215) + reminders/tasks.
    lru_cache → строится один раз; сброс в тестах через _clear_ontology_cache_for_tests()."""
    roots: dict[str, tuple[str, ...]] = {k: tuple(v) for k, v in FAMILY_ROOTS.items()}
    roots["reminders"] = _SEC_REMINDER_ROOTS
    roots["tasks"] = _SEC_TASK_ROOTS
    roots["checklists"] = tuple(roots.get("checklists", ())) + _SEC_CHECKLIST_ROOTS
    return {
        "roots": roots,
        "words": {"checklists": _SEC_CHECKLIST_WORDS, "memory": _MEMORY_WORDS},
        "exact": {k: tuple(v) for k, v in FAMILY_EXACT_ROOTS.items()},
        "lazy": frozenset(LAZY_FAMILIES),
    }


def _clear_ontology_cache_for_tests() -> None:
    """Сброс кэша онтологии (изоляция тестов / monkeypatch). R1-ревью: mutable global без сброса."""
    _ontology.cache_clear()


def _prio(d: str) -> int:
    """Индекс приоритета; неизвестный домен (дрейф онтологии) → в конец (НЕ ValueError, R1-ревью)."""
    return _DOMAIN_PRIORITY.index(d) if d in _DOMAIN_PRIORITY else len(_DOMAIN_PRIORITY)


def _match_domains(toks: list[str]) -> dict[str, int]:
    onto = _ontology()
    out: dict[str, int] = {}
    for dom, rts in onto["roots"].items():
        exact = onto["exact"].get(dom, ())
        words = onto["words"].get(dom, frozenset())
        n = sum(1 for t in toks if t in exact or t in words or any(t.startswith(r) for r in rts))
        if n:
            out[dom] = n
    return out


def route_domains(text: str) -> RouteResult:
    """#221 Ф1: текст → разделы (классы улик) + проекции + директива. Чистая функция (без графа).
    Мульти-домен ТОЛЬКО при союзе МЕЖДУ клаузами; longest-match через фразы; intent_hint — только от
    task-сигнала #197/#215 (НЕ от семейных корней — рефрейм «#197 финален»)."""
    onto = _ontology()
    norm = re.sub(r"чек\s+лист", "чеклист", _normalize(text))  # дефис уже снят _normalize, пробел — здесь

    # intent — авторитет #197/#215: task_signal = явная команда (_must_task, вкл. «что у меня»/«перенеси на»)
    # ИЛИ слово-раздел (_section_hint). Семейные корни (погода/рецепт/купи) сами по себе task НЕ ставят.
    # (Отдельный _INTENT_ONLY_PHRASES убран — дублировал _must_task; широкая «что мне нужно» ловила
    # «что мне нужно ЗНАТЬ про X» → ложный task, R2 Codex high MAJOR.)
    task_signal = _must_task(text) or _section_hint(text) is not None

    clauses = _CONNECTORS_RE.split(norm)
    connector_present = len(clauses) > 1

    # Резолвинг клаузы: action-домен (глагол-действие) ВЫШЕ фразы и content; фраза — поверх голых корней.
    # «Командность» клаузы выводится из ТОГО ЖЕ task-сигнала #197/#215 (_must_task ∪ _section_hint), НЕ из
    # ручного списка глаголов (R4 Codex high/medium: список одновременно ловил инфинитив в теле напоминания
    # «напомни ... и купить ...» → ложный compound, И пропускал «внеси/запланируй» → терял команду).
    clause_info: list[tuple[str, bool]] = []  # (primary раздела клаузы, is_command)
    suppressed: set[str] = set()
    for ct in clauses:
        matched = _match_domains(_WORD_RE2.findall(ct))
        if not matched:
            continue
        action = sorted((d for d in matched if d in _ACTION_DOMAINS), key=_prio)
        forced = next((dom for ph, dom in _PHRASES if ph in ct), None)
        if action:
            primary = action[0]
        elif forced:
            primary = forced
        else:
            primary = sorted(matched, key=lambda d: (_prio(d), -matched[d]))[0]
        suppressed |= {d for d in matched if d != primary}
        clause_info.append((primary, _must_task(ct) or _section_hint(ct) is not None))

    if not clause_info:
        return RouteResult(None, (), (), False, ("task" if task_signal else None),
                           bool(task_signal), (), None, ())

    # compound — ТОЛЬКО при ≥2 РАЗНЫХ клаузах-КОМАНДАХ (task-сигнал); хвост-список без сигнала — не команда.
    command_doms: list[str] = []
    for p, is_cmd in clause_info:
        if is_cmd and p not in command_doms:
            command_doms.append(p)
    all_clause_doms: list[str] = []
    for p, _ in clause_info:
        if p not in all_clause_doms:
            all_clause_doms.append(p)

    if connector_present and len(command_doms) >= 2:
        primary, secondary, compound = command_doms[0], tuple(command_doms[1:]), True
        suppressed |= {d for d in all_clause_doms if d not in command_doms}
    elif command_doms:  # ровно одна команда → ОНА primary (не ронять явную команду, даже если она не первая)
        primary, secondary, compound = command_doms[0], (), False
        suppressed |= {d for d in all_clause_doms if d != command_doms[0]}
    else:
        primary, secondary, compound = all_clause_doms[0], (), False
        suppressed |= set(all_clause_doms[1:])

    suppressed -= {primary, *secondary}
    all_domains = (primary, *secondary)
    active = tuple(sorted(d for d in all_domains if d in onto["lazy"]))
    return RouteResult(primary, secondary, tuple(sorted(suppressed)), compound,
                       ("task" if task_signal else None), False, active,
                       _DIRECTIVE.get(primary), all_domains)


def chat_fact_system_prompt(today_str: str) -> str:
    """Scoped системный промпт для chat/fact (Codex high R2): БЕЗ productivity-инструментов, honesty,
    анти-флейл, web-only. Не перечисляет reminders/tasks/lists — модель не пытается их звать."""
    return (
        "Ты — Среда: тёплый, внимательный личный помощник в мессенджере. Сейчас обычный разговор или "
        "вопрос (не задача с твоими данными).\n"
        f"Сегодня {today_str}.\n"
        "Правила:\n"
        "- Отвечай по-русски, по-доброму и кратко, как живой собеседник.\n"
        "- Инструменты доступны ТОЛЬКО для веба: web_search (поиск), fetch_url (открыть страницу), "
        "get_weather (погода). Других инструментов сейчас нет — не обещай напоминаний/задач/списков.\n"
        "- Если нужен свежий или проверяемый факт — сделай ОДИН web_search и ответь из найденного.\n"
        "- Если не знаешь и поиск не помог — ЧЕСТНО скажи, что не уверена, и НЕ выдумывай детали "
        "(имена, даты, числа, сюжет). Честное «не знаю» лучше выдумки.\n"
        "- На игру/викторину отвечай словами по существу, не пытайся «искать ответ по кругу»."
    )
