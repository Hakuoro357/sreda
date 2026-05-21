"""Post-output deterministic guard для time-of-day greetings.

LLM (особенно mimo-v2.5) известна тем что игнорирует prompt-level instructions
в заметной доле проверок. Реальный prod incident
2026-05-18: bot 3 раза подряд написал «спокойной ночи» в 15:44 MSK
(time-of-day mismatch).

Это deterministic post-processor: regex find greetings, compare с computed
period от current time, при mismatch → strip фразу (или замена на нейтральное).

Carve-out: если user_text **явно просил** именно такое приветствие — НЕ strip.
"""
from __future__ import annotations

import re
from typing import Final


# ─── Period classification ────────────────────────────────────────────

# 5-11 утро / 12-17 день / 18-22 вечер / 23,0-4 ночь.
# Reviewer (Qwen) flagged для elderly Russian speakers «добрый вечер»
# реально с 17:00. Не сдвигаем для cultural-fit полностью, но включаем
# вечер уже с 17 — обработаем 17:00 как "вечер" даже на day-cutoff.
def classify_period(hour: int) -> str:
    """Hour 0..23 → period name."""
    if 5 <= hour <= 11:
        return "утро"
    if 12 <= hour <= 17:
        return "день"
    if 18 <= hour <= 22:
        return "вечер"
    return "ночь"  # 23, 0, 1, 2, 3, 4


# ─── Greeting regexes ─────────────────────────────────────────────────

# Match'ит time-of-day greetings (case-insensitive). Каждый pattern
# имеет capture group (period_word) для проверки соответствия.
#
# Покрытие:
# - «доброе утро/день», «добрый вечер»
# - «спокойной ночи» / «доброй ночи»
# - «ночь на дворе»
_GREETING_PATTERNS: Final = [
    # «Доброе утро/день» (neuter) — adjective changes form per period
    (re.compile(r"\b[Дд]обр(?:ое|ого|ому|ым|ом)\s+(утра|утро|утром|дня|день|днём|днем)\b"), "_adj_to_period"),
    # «Добрый вечер» (masculine)
    (re.compile(r"\b[Дд]обр(?:ый|ого|ому|ым|ом)\s+(вечер|вечера|вечером)\b"), "_adj_to_period"),
    # «Спокойной/доброй ночи»
    (re.compile(r"\b(?:[Сс]покойной|[Дд]оброй)\s+(ночи)\b"), "_adj_to_period"),
    # «Ночь на дворе» / «ночь уже»
    (re.compile(r"\b[Нн]очь\s+(?:на\s+дворе|уже|сейчас|вокруг)\b"), "_implicit_night"),
]

# Mapping word stem → period
_WORD_TO_PERIOD: Final = {
    "утр": "утро", "ден": "день", "дн": "день",
    "вечер": "вечер", "ночи": "ночь",
}


def _word_to_period(word: str) -> str | None:
    """«утро/утра/утром» → «утро» и т.п."""
    w = word.lower()
    for stem, period in _WORD_TO_PERIOD.items():
        if w.startswith(stem):
            return period
    return None


# ─── Carve-out: user явно просил greeting ──────────────────────────────

_USER_GREETING_REQUEST: Final = re.compile(
    r"\b("
    r"пожела(?:й|йте|ю)\s+(?:мне|нам)?\s*(?:доброг[ао]|спокойн[ыойе])"
    r"|скажи\s+(?:доброе\s+утро|добрый\s+вечер|спокойной\s+ночи)"
    r"|поздорова(?:йся|ться)"
    r")",
    re.IGNORECASE,
)


def _user_explicitly_asked_for_greeting(user_text: str) -> bool:
    """True если user явно попросил пожелание/приветствие."""
    if not user_text:
        return False
    return bool(_USER_GREETING_REQUEST.search(user_text))


# ─── Main validator ───────────────────────────────────────────────────


def strip_misaligned_greetings(
    text: str, *, period: str, user_text: str = "",
) -> tuple[str, dict[str, int]]:
    """Удалить time-of-day greetings которые не соответствуют period.

    Args:
        text: bot output ready-to-send to user.
        period: current period — "утро" / "день" / "вечер" / "ночь".
        user_text: user сообщение (для carve-out: явная просьба).

    Returns:
        (clean_text, stats) где stats:
          - mismatched_stripped: int — сколько greeting фраз удалено
          - explicit_request: bool — был ли carve-out на explicit request
    """
    stats: dict[str, int] = {"mismatched_stripped": 0, "explicit_request": 0}
    if not text:
        return text, stats

    if _user_explicitly_asked_for_greeting(user_text):
        stats["explicit_request"] = 1
        return text, stats

    # Iterate patterns, collect spans для удаления (offsetы не должны
    # invalidate на следующей итерации — собираем все replacements
    # и применяем за один pass через re.sub с callback).

    def _make_replacer(implicit_period: str | None):
        def replace(m: re.Match) -> str:
            matched_text = m.group(0)
            # m.lastindex == 1 if pattern имеет group(1), иначе None
            if m.lastindex is not None and m.lastindex >= 1:
                inferred = _word_to_period(m.group(1))
            else:
                inferred = implicit_period
            if inferred and inferred != period:
                stats["mismatched_stripped"] += 1
                return ""  # strip
            return matched_text
        return replace

    new_text = text
    for pattern, kind in _GREETING_PATTERNS:
        implicit = "ночь" if kind == "_implicit_night" else None
        new_text = pattern.sub(_make_replacer(implicit), new_text)

    # Cleanup двойных пробелов и stray punctuation после strip
    new_text = re.sub(r"\s{2,}", " ", new_text)
    new_text = re.sub(r"\s+([.,!?])", r"\1", new_text)
    new_text = new_text.strip()

    return new_text, stats
