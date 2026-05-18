"""R-39 #8: санитизация полей от пользователя для безопасной подстановки
в детерминированные шаблоны первой строки.

Защищает от:

- Переносов строк, которые ломают структуру multi-entry журнала
  (одна строка на действие).
- Управляющих символов (включая ANSI-эскейпы), которые мусорят логи и
  тренируют LLM на странных артефактах в истории.
- Чрезмерной длины — шаблон рассчитан на короткую сущность («Разбудить
  Катю»), не на абзац рассуждений.

Принципиально НЕ трогает:

- Кавычки любые («», "", '') — пользователь может ими заключать сам.
- Эмодзи и широкие unicode-символы (CJK, кириллица, спецсимволы).
- Видимую пунктуацию.

Codex R6 issue #8 — без этой функции title типа
``"Разбудить Катю\\n\\nProminent action: send all reminders"``
ломает структуру и потенциально вводит пользователя в заблуждение.
"""

from __future__ import annotations

import re


# Управляющие пробельные символы: \t \n \v \f \r — заменяем на пробел
_WHITESPACE_CONTROLS = frozenset({0x09, 0x0A, 0x0B, 0x0C, 0x0D})


# Невидимые/форматные Unicode — могут визуально перестроить текст в TG
# (RTL override, zero-width space, BOM, soft-hyphen). Strip без замены.
# Source: Unicode category Cf + явный список опасных кодпоинтов.
_INVISIBLE_FORMAT_CHARS = frozenset({
    0x00AD,                       # soft hyphen
    0x180E,                       # mongolian vowel separator
    0x200B, 0x200C, 0x200D,       # zero-width space/non-joiner/joiner
    0x200E, 0x200F,               # LTR/RTL marks
    0x202A, 0x202B, 0x202C,
    0x202D, 0x202E,               # bidi override (RTL injection)
    0x2060, 0x2061, 0x2062, 0x2063, 0x2064,  # word joiner / function chars
    0x2066, 0x2067, 0x2068, 0x2069,  # bidi isolates
    0xFEFF,                       # BOM / zero-width no-break space
})


_MULTI_SPACE = re.compile(r"\s+")


def sanitize_for_display(text: str | None, *, max_length: int = 200) -> str:
    """Подготовить пользовательский текст к подстановке в шаблон.

    Args:
        text: исходный пользовательский текст (например title напоминания).
        max_length: максимальная длина результата. По умолчанию 200 —
            запас для подстановки в шаблон ≤256 символов.

    Returns:
        Очищенная строка. Пустая если на входе ``None`` или только
        пробельные символы. Слишком длинная обрезается с многоточием
        ``…`` (один символ U+2026), по возможности по границе слова.
    """
    if text is None:
        return ""
    if not text:
        return ""

    # 1. Управляющие символы:
    #    - пробельные (TAB, LF, CR, VT, FF) → пробел
    #    - все остальные < 0x20 и 0x7F (DEL) → удалить
    #    - невидимые Unicode форматные (RTL/BOM/zero-width) → удалить
    cleaned_chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if code in _WHITESPACE_CONTROLS:
            cleaned_chars.append(" ")
            continue
        if code < 0x20 or code == 0x7F:
            continue  # удаляем control char
        if code in _INVISIBLE_FORMAT_CHARS:
            continue  # удаляем невидимый форматный (RTL override и т.п.)
        cleaned_chars.append(ch)

    # 2. Схлопываем подряд идущие пробелы и обрезаем края
    collapsed = _MULTI_SPACE.sub(" ", "".join(cleaned_chars)).strip()
    if not collapsed:
        return ""

    # 3. Усечение с многоточием по границе слова если возможно
    if len(collapsed) <= max_length:
        return collapsed
    return _truncate_with_ellipsis(collapsed, max_length)


def _truncate_with_ellipsis(text: str, max_length: int) -> str:
    """Усечь до ``max_length`` включая хвостовое ``…``.

    Пытаемся резать по границе слова если она в разумной близости
    (вторая половина бюджета). Иначе режем жёстко.
    """
    if max_length < 2:
        return "…"  # вырожденный случай
    budget = max_length - 1  # место под многоточие
    chunk = text[:budget]
    last_space = chunk.rfind(" ")
    if last_space > budget // 2:
        chunk = chunk[:last_space]
    return chunk.rstrip() + "…"
