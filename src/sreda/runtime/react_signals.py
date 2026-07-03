"""#285 Фаза B срез B1: детерминированные сигнальные детекторы (чистые функции).

Единый путь ReAct, пилляр 1/3. Три детектора, калиброванные корпусами
(`plans/285-signal-corpora-v0.md`); тесты кодируют все red-кейсы плана:

- `write_command_signal(text)` → есть ли ЯВНАЯ императивная команда-мутация. ВЫСОКАЯ ТОЧНОСТЬ
  (false-positive = молчаливая запись без confirm — ярус (а) плана): срабатывает только на
  чёткий императив, промахи (65% ходов, базлайн) ловит ярус (б) confirm. НЕ `_section_hint`
  (тот даёт «как дела»→checklists — мина R1-субагента Фазы 0).
- `declarative_memory_signal(text)` → декларативный stable-fact о себе (→ save_core_fact без
  confirm). Высокая точность: near-miss «меня зовут на дачу» НЕ срабатывает (заглавная у имени).
- `read_cue_domains(text)` → щедрый кюс→bounded-домены (пилляр 3: цена ошибки = лишний read в
  бюджете, не мутация). Идиомы («как дела») исключены НА УРОВНЕ ФРАЗЫ, не слова.

Домены-на-ЗАПИСЬ из сигнала резолвит B2 (через route_domains-онтологию + эти детекторы) — здесь
только «есть ли сигнал» + «какие read-домены». Метаданные доменов/инструментов — B2.
"""

from __future__ import annotations

import re

# ── команды-мутации (императив 2л.ед.ч.). Паразиты отсечены negative-lookahead
# (базлайн Фазы 0 v2: «удалось/удалились», «поставщик», «внесли (не внеси)»).
# Точность важнее полноты: сомнительное НЕ включаем (промах → ярус (б) confirm).
_CMD_VERBS = re.compile(
    r"\b("
    r"добавь|добавить|сохран(и|ить)|запиш(и|ать)|записать"
    r"|заплан(ируй|ировать)|внеси|внесите|отмет(ь|ить)|вычеркн(и|уть)"
    r"|поставь(?=\s)|создай|создать|отмени(?=\s|ть)|перенес(и|ти)"
    r"|напомни|запомни|заведи"
    r"|удал(и|ить|ите)(?!ось|ась|ились|ился)"
    r"|куп(и|ить)(?=\s)"
    r")\b",
    re.IGNORECASE,
)

# ── декларативные stable-fact паттерны (→ память). Заглавная у ИМЕНИ/места отсекает near-miss
# («меня зовут на дачу», «живу надеждой»). НЕ глобальный IGNORECASE — только первое слово
# может быть с заглавной (начало предложения).
_DECL_FACT = (
    re.compile(r"\b[Мм]еня зовут\s+[А-ЯЁ][а-яё]+"),
    re.compile(r"\b(?:[Яя]\s+)?[Жж]иву в\s+[А-ЯЁ]"),
    re.compile(r"\b[Уу] меня\s+(?:двое|трое|четверо|пятеро|\d+)\s*(?:дет|реб|сын|доч)"),
    re.compile(r"\b(?:мо(?:его|ю|ей)\s+)?(?:муж[аеу]?|жену?|сына?|доче?[рь]|дочку|"
               r"кота|собаку|мам[уы]|пап[уы])\s+зовут\s+[А-ЯЁ]"),
    re.compile(r"\bмо[её]\s+имя\s+[—-]?\s*[А-ЯЁ]"),
)

# ── read-кюсы → bounded домены (щедро; цена ошибки — лишний read в бюджете). Идиомы исключаются
# ПЕРЕД проверкой кюсов (иначе «как дела»→checklists — мина).
_READ_IDIOMS = re.compile(
    r"\bкак\s+(?:дела|жизнь|ты|вы|оно|сам|делишки|настроени)"
    r"|\bчто\s+нового|\bкак\s+сам|\bдоброе\s+утро|\bдобрый\s+(?:день|вечер)",
    re.IGNORECASE,
)
# кюс → множество доменов (bounded). Порядок проверки не важен — объединяем все совпадения.
_READ_CUES: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (re.compile(r"\bнапоминани", re.IGNORECASE), frozenset({"reminders"})),
    (re.compile(r"\bзадач", re.IGNORECASE), frozenset({"tasks"})),
    (re.compile(r"\b(?:спис(?:ок|ка|ке|ки)|дела\b|дел\b|чеклист|чек-лист)", re.IGNORECASE),
     frozenset({"checklists", "shopping"})),
    (re.compile(r"\b(?:покупк|купить\s+список)", re.IGNORECASE), frozenset({"shopping"})),
    (re.compile(r"\b(?:как\s+меня\s+зовут|помн(?:ишь|ю)|что\s+я\s+(?:говорил|рассказыв)"
                r"|что\s+у\s+меня\s+записано|мо[её]\s+имя)", re.IGNORECASE),
     frozenset({"memory"})),
    (re.compile(r"\b(?:мен[ю юе]\b|меню)", re.IGNORECASE), frozenset({"menu"})),
    (re.compile(r"\bрецепт", re.IGNORECASE), frozenset({"recipes"})),
)


def write_command_signal(text: str) -> bool:
    """Есть ли ЯВНАЯ императивная команда-мутация (ярус (а): → allowed_write без confirm).
    High-precision: промах безопасен (ярус (б) confirm). Пустой/None → False."""
    return bool(_CMD_VERBS.search(text or ""))


def declarative_memory_signal(text: str) -> bool:
    """Декларативный stable-fact о себе (→ save_core_fact без confirm). near-miss отсечён заглавной."""
    t = text or ""
    return any(p.search(t) for p in _DECL_FACT)


def read_cue_domains(text: str) -> frozenset[str]:
    """Щедрый кюс→bounded read-домены (пилляр 3). Идиома («как дела») → пусто (не own-data read).
    Пусто = baseline (web без own-data, решает B2). Возврат — объединение доменов совпавших кюсов."""
    t = text or ""
    if _READ_IDIOMS.search(t):
        return frozenset()
    out: set[str] = set()
    for pat, doms in _READ_CUES:
        if pat.search(t):
            out |= doms
    return frozenset(out)
