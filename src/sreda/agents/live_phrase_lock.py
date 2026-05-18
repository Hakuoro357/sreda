"""R-39: замок живой фразы — три правила (Codex/Qwen R6 закрыли).

Живая фраза — короткая тёплая фраза которую LLM пишет ПОСЛЕ
детерминированной первой строки подтверждения. Замок — pure-validator
который пропускает только безопасные фразы:

1. **Длина** ≤ 120 символов. Длиннее — это не живая фраза, а параграф.
2. **Нет claim verbs.** Глаголы вроде «поставила/отменила/сохранила»
   — фактические утверждения о действии. Они уже сказаны в первой
   строке; повтор от LLM = опасное место где модель может соврать.
3. **Нет новых сущностей.** Числа / время / квоты / имена в фразе
   должны уже присутствовать в первой строке. Если LLM ввела
   «приду в 16:00» а первая строка про 14:00 — это рассинхрон.

При нарушении любого правила caller отдаёт пользователю первую строку
без живой фразы (+ алерт админу).

Это **последний слой** защиты от confab класса бага — после
структурной (типизированный вывод планировщика), физической
(composer без инструментов) и контрактной (детерминированная первая
строка). Если предыдущие слои дали трещину — замок ловит здесь.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union


# ─── Claim verbs ──────────────────────────────────────────────────────


# Глаголы которые декларируют выполненное действие. Среда говорит в
# женском роде — основной фокус на формах с «-ла». Также включены
# инфинитивы и 1л будущего/настоящего (LLM иногда пишет «поставлю»).
_COMPLETED_TENSE_CLAIM_VERBS = frozenset({
    # поставить
    "поставила", "поставил", "поставлю", "поставим", "ставлю",
    # отменить
    "отменила", "отменил", "отменю", "отменяю", "отменим",
    # сохранить
    "сохранила", "сохранил", "сохраню", "сохраним",
    # добавить
    "добавила", "добавил", "добавлю", "добавим",
    # удалить
    "удалила", "удалил", "удалю", "удалим",
    # записать
    "записала", "записал", "запишу", "запишем",
    # выполнить
    "выполнила", "выполнил", "выполню", "выполним",
    # создать
    "создала", "создал", "создам", "создадим", "сделала", "сделал",
    # перенести
    "перенесла", "перенёс", "перенесу", "перенесем",
    # поменять / изменить
    "поменяла", "поменял", "поменяю", "изменила", "изменил", "изменю",
    # обновить
    "обновила", "обновил", "обновлю",
    # отметить
    "отметила", "отметил", "отмечу",
    # запомнить
    "запомнила", "запомнил", "запомню",
    # забронировать
    "забронировала", "забронировал", "забронирую",
    # отключить/включить
    "включила", "включил", "выключила", "выключил",
    # снять / убрать
    "сняла", "снял", "сниму", "убрала", "убрал",
})


# ─── Результат замка ─────────────────────────────────────────────────


@dataclass(frozen=True)
class LockPass:
    """Фраза прошла все три правила."""


@dataclass(frozen=True)
class LockFail:
    """Фраза провалила одно из правил.

    ``reason``:
      - ``too_long``: длина > max_length
      - ``claim_verb_in_phrase``: найден claim verb
      - ``new_entity_in_phrase``: в фразе сущность которой нет в первой строке
    ``detail``: конкретика (число, имя verb'а, найденная сущность).
    """

    reason: str
    detail: str = ""


LockResult = Union[LockPass, LockFail]


# ─── Извлечение сущностей ────────────────────────────────────────────


# Числа (целые и десятичные), времена ЧЧ:ММ или ЧЧ.ММ, кавычки разных видов.
_ENTITY_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_ENTITY_TIME = re.compile(r"\b\d{1,2}[:.]\d{2}\b")
_ENTITY_QUOTED = re.compile(r"[«\"']([^«»\"']+)[»\"']")
# Заглавная кириллическая последовательность ≥3 символов (имя собственное)
_ENTITY_PROPER = re.compile(r"\b[А-ЯЁ][а-яё]{2,}")


def _is_sentence_start(text: str, pos: int) -> bool:
    """Слово в позиции ``pos`` стоит в начале предложения?

    Начало предложения — заглавная буква не означает proper name
    (например «Как раз перед обедом» — «Как» не имя). Считаем sentence-start
    если перед нами начало строки или конец предыдущего предложения
    (``.!?``) c необязательным пробелом.
    """
    if pos == 0:
        return True
    # Идём назад до первого non-whitespace символа
    i = pos - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    if i < 0:
        return True
    return text[i] in ".!?"


def _extract_entities(text: str) -> tuple[set[str], set[str]]:
    """Извлечь сущности фразы. Возвращает (точные сущности, proper-name корни).

    Точные сущности (числа, времена, цитаты) проверяются по полному
    совпадению. Имена собственные сравниваются по 4-символьному корню
    (морфология: «Катю» / «Катей» — общий корень «катю»/«кате»;
    короткий 3-char prefix даёт false-positive «Каток» vs «Катю»).

    Trade-off: 4 chars не покроет очень короткие имена (Ян, Ия) и
    редкие случаи когда форма имени отличается в первой 4-х буквах
    (Катя/Катю — общие первые 3). False-fail (заблокируем хорошую
    фразу) считаем меньшим злом чем false-pass (пропустим confab).
    """
    exact: set[str] = set()
    proper_stems: set[str] = set()

    # Сначала времена — фиксируем span'ы чтобы исключить «16» из числа
    # когда оно часть «16:00».
    time_spans: list[tuple[int, int]] = []
    for m in _ENTITY_TIME.finditer(text):
        exact.add(m.group(0).replace(".", ":"))
        time_spans.append(m.span())

    def _inside_time_span(pos: int, end: int) -> bool:
        return any(s <= pos and end <= e for s, e in time_spans)

    for m in _ENTITY_NUMBER.finditer(text):
        if _inside_time_span(m.start(), m.end()):
            continue
        exact.add(m.group(0))
    for m in _ENTITY_QUOTED.finditer(text):
        exact.add(m.group(1).strip().lower())
    for m in _ENTITY_PROPER.finditer(text):
        if _is_sentence_start(text, m.start()):
            continue
        word = m.group(0).strip().lower()
        if len(word) >= 4:
            proper_stems.add(word[:4])
        elif len(word) == 3:
            # Слово ровно 3 символа — используем как есть (нет ресурса
            # для морфологии). Реже даёт false-fail чем 4-char для 3-х
            # символных имён.
            proper_stems.add(word)

    return exact, proper_stems


# ─── Нормализация ────────────────────────────────────────────────────


_PUNCT_TO_SPACE = re.compile(r"[.,!?;:()\[\]«»\"'—–\-]+")
_MULTI_SPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Привести к нижнему регистру, заменить пунктуацию на пробел."""
    out = _PUNCT_TO_SPACE.sub(" ", text.lower())
    return _MULTI_SPACE.sub(" ", out).strip()


# ─── Главная функция замка ───────────────────────────────────────────


def validate_live_phrase(
    phrase: str,
    first_line: str,
    *,
    max_length: int = 120,
) -> LockResult:
    """Замок живой фразы — три правила.

    Args:
        phrase: живая фраза от LLM (ожидается ≤120 символов).
        first_line: уже-собранная детерминированная первая строка
            (содержит факты — название действия, время, имя). Все
            сущности фразы должны быть в ней.
        max_length: лимит длины (по умолчанию 120).

    Returns:
        ``LockPass`` если все три правила выполнены, ``LockFail`` с
        указанием причины и деталей если хоть одно нарушено.

    Особый случай: пустая фраза (или только пробелы) считается
    допустимой — caller сам решит показывать что-то или нет.
    """
    stripped = phrase.strip()
    if not stripped:
        return LockPass()

    # Правило 1: длина
    if len(stripped) > max_length:
        return LockFail(reason="too_long", detail=f"{len(stripped)} > {max_length}")

    # Правило 2: claim verbs
    phrase_norm = _normalize(stripped)
    for word in phrase_norm.split():
        if word in _COMPLETED_TENSE_CLAIM_VERBS:
            return LockFail(reason="claim_verb_in_phrase", detail=word)

    # Правило 3: новые сущности
    first_norm = _normalize(first_line)
    phrase_exact, phrase_proper_stems = _extract_entities(stripped)
    first_exact, _ = _extract_entities(first_line)

    # Точные сущности (числа, времена, цитаты) — должны совпасть
    for entity in phrase_exact:
        if entity in first_exact:
            continue
        if entity in first_norm:
            continue
        return LockFail(reason="new_entity_in_phrase", detail=entity)

    # Имена собственные — морфологический stem-match (3 chars)
    if phrase_proper_stems:
        first_words = first_norm.split()
        for stem in phrase_proper_stems:
            if any(w.startswith(stem) for w in first_words):
                continue
            return LockFail(reason="new_entity_in_phrase", detail=stem)

    return LockPass()
