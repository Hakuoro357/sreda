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
