"""Post-output deterministic guard для date drift в bot replies.

Prod incident 2026-05-18 (tenant_max_142322319):
  USER: «Сахар 10,2.»  (timestamp 2026-05-18 05:52 UTC)
  BOT:  «Записала: 17.05.2026, утро — сахар 10.2»     ← вчера, а не сегодня

System prompt уже содержит «ISO дата 'сегодня': 2026-05-18» (R-34 fix),
но mimo всё равно использовала вчерашнюю. Codex+Qwen review: добавить
deterministic post-output validator (Qwen M-2, Codex CRITICAL: «нельзя
оставлять на prompt»).

Этот модуль ищет DD.MM[.YYYY] или DD <месяц> упоминания в bot reply.
Если найденная дата:
  (a) в прошлом (≤ today - 1 day), AND
  (b) user_text НЕ упоминал её эксплицитно (carve-out),

→ flag в логе для admin review (WARNING). Strip фразу не делаем сразу
(date references часто встроены в осмысленный контекст — strip может
сломать предложение); вместо этого injection «🤖 проверь дату» tag
для human moderator alert + дальнейший анализ pattern frequency.

В commit 3 (отдельно) — pre-save validator для structured measurement
writes (там реально strip / reject — это safety-critical для medical data).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Final


# ─── Russian month genitive → number ──────────────────────────────────

_RU_MONTHS: Final = {
    "января":  1, "февраля":  2, "марта":     3, "апреля":  4,
    "мая":     5, "июня":     6, "июля":      7, "августа": 8,
    "сентября":9, "октября": 10, "ноября":   11, "декабря":12,
}


# ─── Date pattern regexes ─────────────────────────────────────────────

# DD.MM.YYYY (Russian numeric format)
_DATE_DMY_RE: Final = re.compile(
    r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b",
)

# DD.MM-short (без года) НЕ покрывается — слишком много false positives
# с decimal measurements ("сахар 10.2", "давление 12.5"). Bot обычно
# пишет даты либо с годом (17.05.2026), либо worded ("17 мая").
# Если позже понадобится — добавим с context-aware filtering.

# DD <месяц-genitive> ([YYYY])
_DATE_WORDED_RE: Final = re.compile(
    r"\b(\d{1,2})\s+("
    + "|".join(_RU_MONTHS.keys())
    + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)

# Relative references — у user'a и в reply это OK если matched в user_text
_RELATIVE_RE: Final = re.compile(
    r"\b(вчера|позавчера|сегодня|завтра|послезавтра)\b", re.IGNORECASE,
)

# audit-fix 2026-07-18 (svc-ops MINOR #7): слово → смещение от «сегодня».
# Раньше ЛЮБОЕ из этих слов в user_text оправдывало ЛЮБУЮ прошедшую дату:
# «напомни завтра про лекарство» + ответ бота со вчерашней датой НЕ
# флаговался — а это ровно прод-инцидент 2026-05-18, ради которого модуль
# написан. Теперь carve-out только когда target_date соответствует
# конкретному слову.
_RELATIVE_WORD_OFFSETS: Final = {
    "позавчера": -2,
    "вчера": -1,
    "сегодня": 0,
    "завтра": 1,
    "послезавтра": 2,
}


# ─── Carve-out: user явно упомянул date ──────────────────────────────


def _user_mentioned_date(user_text: str, target_date: date, today: date) -> bool:
    """True если user явно упомянул эту дату или её relative reference.

    Покрывает:
      - DD.MM, DD.MM.YYYY в user_text matching target
      - DD <месяц> в user_text
      - «вчера»/«позавчера»/«сегодня»/«завтра»/«послезавтра» — ТОЛЬКО когда
        target_date равна конкретной относительной дате от ``today``
        (audit-fix 2026-07-18: раньше любое слово оправдывало любую дату).
    """
    if not user_text:
        return False

    # Численные форматы (только DD.MM.YYYY — DD.MM short удалён)
    for m in _DATE_DMY_RE.finditer(user_text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            if date(y, mo, d) == target_date:
                return True
        except ValueError:
            pass
    # Worded форматы
    for m in _DATE_WORDED_RE.finditer(user_text):
        d = int(m.group(1))
        mo = _RU_MONTHS.get(m.group(2).lower())
        y = int(m.group(3)) if m.group(3) else target_date.year
        if mo is None:
            continue
        try:
            if date(y, mo, d) == target_date:
                return True
        except ValueError:
            pass

    # Relative references — точное соответствие слова целевой дате.
    for m in _RELATIVE_RE.finditer(user_text):
        offset = _RELATIVE_WORD_OFFSETS.get(m.group(1).lower())
        if offset is not None and target_date == today + timedelta(days=offset):
            return True

    return False


# ─── Main validator ───────────────────────────────────────────────────


def find_drifted_dates(
    text: str, *, iso_date_today: str, user_text: str = "",
) -> list[dict]:
    """Найти даты в past которые user не упомянул.

    Args:
        text: bot reply.
        iso_date_today: «2026-05-18» — current date в user_tz.
        user_text: для carve-out.

    Returns:
        Список dict'ов:
          {date: date, raw: str, span: (s,e), days_ago: int}
        Только prior dates (≤ today-1).
    """
    if not text or not iso_date_today:
        return []

    try:
        today = date.fromisoformat(iso_date_today)
    except (ValueError, TypeError):
        return []

    findings: list[dict] = []

    # DD.MM.YYYY
    for m in _DATE_DMY_RE.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            target = date(y, mo, d)
        except ValueError:
            continue
        if target >= today:
            continue  # сегодня или будущее — ok (reminder)
        if _user_mentioned_date(user_text, target, today):
            continue
        findings.append({
            "date": target, "raw": m.group(0), "span": m.span(),
            "days_ago": (today - target).days,
            "format": "dmy",
        })

    # DD <месяц>
    for m in _DATE_WORDED_RE.finditer(text):
        d = int(m.group(1))
        mo_name = m.group(2).lower()
        mo = _RU_MONTHS.get(mo_name)
        y = int(m.group(3)) if m.group(3) else today.year
        if mo is None:
            continue
        try:
            target = date(y, mo, d)
        except ValueError:
            continue
        if target >= today:
            continue
        if _user_mentioned_date(user_text, target, today):
            continue
        findings.append({
            "date": target, "raw": m.group(0), "span": m.span(),
            "days_ago": (today - target).days,
            "format": "worded",
        })

    return findings
