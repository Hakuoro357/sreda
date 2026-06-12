"""#124 срез 2 — обоснованность пункта в источнике (анти-фабрикация).

Механический барьер класса 9/9: пункт добавляемого списка обязан
встречаться в показанном пользователю материале (текущее сообщение или
блок недавних реплик), иначе планировщик его выдумал → честное
уточнение, не запись.

Сверка — НЕ substring (принимал «рис» в «ирис», «соль» в «фасоль» —
Codex план R2/R3), а непрерывная последовательность ТОКЕНОВ С ГРАНИЦАМИ
в ОДНОЙ реплике-источнике. Нормализатор и матчер — общий код для
runtime-гейта и тестов.
"""

from __future__ import annotations

import re
import unicodedata

# токен = последовательность букв/цифр; пунктуация и пробелы — границы
# внутрисловный дефис — НЕ граница токена (Codex R1: иначе выдуманный
# «песок» обосновывался в «сахар-песок», «то» в «что-то»)
_TOKEN_RE = re.compile(r"[^\W_]+(?:[-‐‒–—][^\W_]+)*",
                       re.UNICODE)


def normalize_for_match(text: str) -> str:
    """Канонизация для сверки: NFKC, NBSP/пробелы схлопнуть, casefold,
    ё→е, краевая пунктуация убрана. БЕЗ семантических синонимов."""
    if not text:
        return ""
    # NFKC сперва — компонует разложенные ё/й (е+◌̈ → ё, и+◌̆ → й).
    t = unicodedata.normalize("NFKC", text)
    # удалить невидимые формат-символы (Cf: zero-width) и оставшиеся
    # комбинирующие знаки (Mn) — иначе разрывают токен (Codex R1 оба
    # MAJOR: невидимый разрыв внутри слова обосновывал фрагмент).
    t = "".join(ch for ch in t
                if unicodedata.category(ch) not in ("Cf", "Mn"))
    t = t.casefold().replace("ё", "е")
    t = re.sub(r"\s+", " ", t).strip()       # \s в Unicode ловит NBSP
    t = t.strip(".,!?;:…\"'«»()[]—–")          # краевая пунктуация (не дефис)
    return t.strip()


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize_for_match(text))


def _contains_subsequence(haystack: list[str], needle: list[str]) -> bool:
    """needle — непрерывная подпоследовательность токенов haystack."""
    if not needle:
        return False
    n, m = len(haystack), len(needle)
    for i in range(n - m + 1):
        if haystack[i:i + m] == needle:
            return True
    return False


def item_grounded_in_sources(item: str, sources: list[str]) -> bool:
    """True, если токены ``item`` встречаются непрерывной
    последовательностью С ГРАНИЦАМИ хотя бы в ОДНОМ источнике.

    Каждый источник — отдельная реплика/сообщение; матч через границы
    разных источников НЕ засчитывается (Codex: «молоко хлеб» из двух
    реплик — не обоснован)."""
    item_tokens = _tokens(item)
    if not item_tokens:
        return False
    for src in sources:
        if _contains_subsequence(_tokens(src), item_tokens):
            return True
    return False
