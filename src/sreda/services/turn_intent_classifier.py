"""R-39: классификатор намерения хода разговора.

Различает 4 типа:
- MUTATION — пользователь хочет записать/изменить/удалить
- READ — пользователь хочет прочитать данные
- CHITCHAT — болтовня без действия
- UNCERTAIN — паттерн не распознан

Целевые метрики (на калибровочном корпусе Day 5):
- mutation: recall ≥98%, precision ≥95%
- read: recall ≥90%
- chitchat: recall ≥90%

Поведение fall-safe: при двусмысленности между chitchat и mutation
вызывающий код может трактовать UNCERTAIN как mutation для безопасности
(лучше зря показать инструмент, чем пропустить нужное действие).

Используется в R-39 для выбора tool_choice:
- mutation → tool_choice='required'
- read     → tool_choice='auto'
- chitchat → tool_choice='none' (или 'auto' с пустым набором)
- uncertain→ tool_choice='auto' (если caller не downgrade'ит до mutation)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class TurnIntent(str, Enum):
    """Намерение хода разговора."""

    MUTATION = "mutation"
    READ = "read"
    CHITCHAT = "chitchat"
    UNCERTAIN = "uncertain"


@dataclass
class TurnClassification:
    """Результат классификации с уверенностью и трассой."""

    intent: TurnIntent
    confidence: float  # 0.0..1.0
    reasons: list[str] = field(default_factory=list)


# ─── Глаголы действия (mutation) ──────────────────────────────────────
# Корни в повелительном или 1л будущем времени. \w* в шаблоне
# подбирает окончания: «поставь», «поставлю», «поставить».

_MUTATION_ROOTS = [
    "постав",      # поставить, поставь, поставлю
    "добав",       # добавить, добавь
    "запиш",       # записать, запиши
    "записыв",     # записывать
    "созда",       # создать, создай
    "отмен",       # отменить, отмени, отменяю
    "удал",        # удалить, удали, удаляю
    "сохран",      # сохранить, сохрани
    "снем",        # снять (формы), сними
    "сним",        # сними
    "перенес",     # перенести, перенеси
    "перенос",     # переносить
    "отмет",       # отметить, отметь
    "купи",        # купить, купи, купил
    "купл",        # куплю, купим, купите (1л/мн.ч. — другая основа)
    "запомн",      # запомнить, запомни
    "сдела",       # сделать, сделай
    "включ",       # включить, включи
    "выключ",      # выключить, выключи
    "напомн",      # напомнить, напомни
    "забу",        # забудь
    "разбуди",     # разбуди
    "позвони",     # позвони
    "проверь",     # проверь (read-ish, но часто пишет)
    "поспеши",     # поспеши
    "поторопи",    # поторопи
    "поменя",      # поменяй, поменять
    "измен",       # измени, изменить
    "обнови",      # обнови, обновить
    "почисти",     # почисти, почистить
    "забронир",    # забронировать
    "отложи",      # отложи, отложить
    "плани",       # планирую, планировать (часто = добавь в план)
]

_MUTATION_VERB_RE = re.compile(
    r"\b(?:" + "|".join(_MUTATION_ROOTS) + r")\w*",
    re.IGNORECASE,
)


def _find_mutation_verb(low: str) -> str | None:
    m = _MUTATION_VERB_RE.search(low)
    return m.group(0) if m else None


# ─── Маркеры коррекции ────────────────────────────────────────────────
# Срабатывают только в паре с time/digit упоминанием.

_CORRECTION_RE = re.compile(
    r"\b(?:"
    r"нет[,.]\s"           # «нет, ...» или «нет. ...»
    r"|не\s+(?:на|в)\s+\d"  # «не на 2», «не в 14»
    r"|не\s+правильн"
    r"|неправильн"
    r"|не\s+так\b"
    r"|не\s+туда\b"
    r"|ошибк"               # ошибка, ошибся, ошибочно
    r"|переигра"            # переиграть
    r"|поменяй\s+на"        # «поменяй на 14»
    r")",
    re.IGNORECASE,
)

# Упоминания времени для активации correction → mutation
_TIME_HINT_RE = re.compile(
    r"\d{1,2}:\d{2}"                              # ЧЧ:ММ
    r"|\d+\s*(?:час|часа|часов|минут|минуты|минуту)"  # N часов / N минут
    r"|\b(?:утра|дня|вечера|ночи)\b"
    r"|\b(?:завтра|сегодня|послезавтра|вчера)\b"
    r"|\bчерез\s+\d"
    r"|\b(?:на|в)\s+\d{1,2}\b",
    re.IGNORECASE,
)


def _find_correction_marker(low: str) -> str | None:
    m = _CORRECTION_RE.search(low)
    return m.group(0).strip() if m else None


def _has_time_mention(low: str) -> bool:
    return bool(_TIME_HINT_RE.search(low))


# ─── Шаблоны чтения ───────────────────────────────────────────────────

_READ_RE = re.compile(
    r"\bпокаж\w*"                          # покажи
    r"|\bчто\s+у\s+меня\b"                # что у меня
    r"|\bчто\s+(?:на|в)\s+(?:завтра|сегодня|спис|план)"
    r"|\bчто\s+запланир"                  # что запланировано
    r"|\bсколько\b"
    r"|\bкакие?\b"
    r"|\bкакой\b"
    r"|\bкакая\b"
    r"|\bкогда\b"
    r"|\bесть\s+ли\b",
    re.IGNORECASE,
)


def _find_read_pattern(low: str) -> str | None:
    m = _READ_RE.search(low)
    return m.group(0) if m else None


# ─── Шаблоны болтовни ─────────────────────────────────────────────────
# Узкие fixed-phrase, чтобы не размазать precision.

_CHITCHAT_RE = re.compile(
    r"\bкак\s+дела\b"
    r"|\bкак\s+ты\b"
    r"|\bкак\s+сам(?:а|и)?\b"
    r"|\bпривет\b"
    r"|\bздрав\w*"
    r"|\bдобр(?:ое|ый)\s+(?:день|вечер|утро|утра)\b"
    r"|\bздарова\b"
    r"|\bспасиб\w*"
    r"|\bблагодар\w*"
    r"|\bпока\b"
    r"|\bдо\s+свидан"
    r"|\bдо\s+встреч"
    r"|^\s*ок\b"
    r"|^\s*хорошо\b"
    r"|^\s*ладно\b"
    r"|^\s*давай\b"
    r"|\bчто\s+дум\w*"
    r"|\bкак\s+считаеш"
    r"|\bкак\s+полагае"
    r"|^\s*да\s*[.!?]*\s*$"
    r"|^\s*нет\s*[.!?]*\s*$"
    r"|^\s*угу\b"
    r"|^\s*ага\b",
    re.IGNORECASE,
)


def _find_chitchat_pattern(low: str) -> str | None:
    m = _CHITCHAT_RE.search(low)
    return m.group(0).strip() if m else None


# ─── Главная функция ──────────────────────────────────────────────────


def classify_turn(text: str) -> TurnClassification:
    """Классифицирует намерение хода разговора по тексту пользователя.

    Args:
        text: текст пользователя (одно сообщение или склейка нескольких)

    Returns:
        TurnClassification с intent, confidence и трассой reasons.

    Порядок проверки:
        1. Mutation verb — главный сигнал, высокий вес
        2. Correction marker + time mention — fall-safe mutation
        3. Read pattern
        4. Chitchat pattern
        5. Uncertain — caller решает (рекомендация: считать mutation
           если предыдущий ход содержал pending action)
    """
    low = (text or "").lower().strip()
    if not low:
        return TurnClassification(
            intent=TurnIntent.CHITCHAT,
            confidence=0.9,
            reasons=["empty_text"],
        )

    reasons: list[str] = []

    # 1. Mutation verb — самый сильный сигнал
    mut = _find_mutation_verb(low)
    if mut:
        reasons.append(f"mutation_verb:{mut}")
        return TurnClassification(
            intent=TurnIntent.MUTATION,
            confidence=0.95,
            reasons=reasons,
        )

    # 2. Correction marker + упоминание времени → mutation
    correction = _find_correction_marker(low)
    if correction:
        reasons.append(f"correction:{correction!r}")
        if _has_time_mention(low):
            reasons.append("time_mention")
            return TurnClassification(
                intent=TurnIntent.MUTATION,
                confidence=0.85,
                reasons=reasons,
            )
        # correction без времени — uncertain, caller разберётся

    # 3. Read pattern
    read = _find_read_pattern(low)
    if read:
        reasons.append(f"read_pattern:{read}")
        return TurnClassification(
            intent=TurnIntent.READ,
            confidence=0.85,
            reasons=reasons,
        )

    # 4. Chitchat pattern
    chit = _find_chitchat_pattern(low)
    if chit:
        reasons.append(f"chitchat:{chit}")
        return TurnClassification(
            intent=TurnIntent.CHITCHAT,
            confidence=0.85,
            reasons=reasons,
        )

    # 5. Uncertain — не подошёл ни один паттерн
    reasons.append("no_clear_pattern")
    return TurnClassification(
        intent=TurnIntent.UNCERTAIN,
        confidence=0.4,
        reasons=reasons,
    )
