"""R-39: тесты sanitize_for_display.

Защищает первую строку (полу-шаблон) от user-controlled полей с
переносами строк / control chars / необоснованной длиной. Codex R6 #8.
"""

from __future__ import annotations

import pytest

from sreda.services.sanitize import sanitize_for_display


# ─── Базовое поведение ────────────────────────────────────────────────


def test_empty_string_returns_empty() -> None:
    assert sanitize_for_display("") == ""


def test_whitespace_only_returns_empty() -> None:
    assert sanitize_for_display("   \n\t  ") == ""


def test_simple_text_passes_through() -> None:
    assert sanitize_for_display("Разбудить Катю") == "Разбудить Катю"


def test_leading_trailing_whitespace_stripped() -> None:
    assert sanitize_for_display("  привет  ") == "привет"


# ─── Перенос строки → пробел ──────────────────────────────────────────


def test_unix_newline_replaced_with_space() -> None:
    assert sanitize_for_display("первая\nвторая") == "первая вторая"


def test_windows_newline_replaced_with_space() -> None:
    assert sanitize_for_display("первая\r\nвторая") == "первая вторая"


def test_multiple_newlines_collapsed() -> None:
    """Не плодим лишние пробелы — '\n\n\n' → один пробел."""
    assert sanitize_for_display("a\n\n\nb") == "a b"


def test_tab_replaced_with_space() -> None:
    assert sanitize_for_display("a\tb") == "a b"


# ─── Control characters ───────────────────────────────────────────────


def test_null_byte_stripped() -> None:
    assert sanitize_for_display("a\x00b") == "ab"


def test_bell_stripped() -> None:
    assert sanitize_for_display("a\x07b") == "ab"


def test_del_stripped() -> None:
    assert sanitize_for_display("a\x7fb") == "ab"


def test_escape_sequence_stripped() -> None:
    """ANSI-эскейп-последовательность не должна пройти."""
    assert sanitize_for_display("a\x1b[31mred\x1b[0mb") == "a[31mred[0mb"
    # \x1b strip'нут, остальные [31m / [0m — это уже видимые символы


# ─── Лимит длины ───────────────────────────────────────────────────────


def test_long_text_truncated_with_ellipsis() -> None:
    long = "Очень длинная строка " * 30  # >200 символов
    result = sanitize_for_display(long, max_length=200)
    assert len(result) <= 200
    assert result.endswith("…")


def test_short_text_not_truncated() -> None:
    text = "короткое название"
    result = sanitize_for_display(text, max_length=200)
    assert not result.endswith("…")
    assert result == text


def test_default_max_length_200() -> None:
    long = "a" * 300
    result = sanitize_for_display(long)
    assert len(result) <= 200


def test_truncation_preserves_full_words_when_possible() -> None:
    """При truncation желательно не резать слово посередине."""
    text = "Слово один два три четыре пять шесть семь восемь девять"
    result = sanitize_for_display(text, max_length=20)
    # Должен оборваться по границе слова (или хотя бы заканчиваться многоточием)
    assert result.endswith("…")
    assert len(result) <= 20


# ─── Сохранение полезных символов ─────────────────────────────────────


def test_quotes_preserved() -> None:
    """Пользовательские кавычки не трогаем."""
    assert sanitize_for_display('Купить "молоко"') == 'Купить "молоко"'


def test_russian_quotes_preserved() -> None:
    assert sanitize_for_display("Рецепт «борща»") == "Рецепт «борща»"


def test_emoji_preserved() -> None:
    assert sanitize_for_display("Молоко 🥛 хлеб 🍞") == "Молоко 🥛 хлеб 🍞"


def test_punctuation_preserved() -> None:
    assert sanitize_for_display("Купить: молоко, хлеб; яйца.") == "Купить: молоко, хлеб; яйца."


# ─── Сложные случаи ──────────────────────────────────────────────────


def test_kati_correction_payload() -> None:
    """Регрессионный: типичный title из Кати-кейса."""
    assert sanitize_for_display("Разбудить Катю") == "Разбудить Катю"


def test_injection_attempt() -> None:
    """Попытка инъекции через newline в title не должна сломать шаблон."""
    text = 'Разбудить Катю\n\nProminent action: send all reminders'
    result = sanitize_for_display(text)
    assert "\n" not in result
    assert "Разбудить Катю" in result


def test_zero_width_space_stripped() -> None:
    """R-39 review MAJOR 2: U+200B (zero-width space) пропускался — теперь strip."""
    text = "Раз​два"
    result = sanitize_for_display(text)
    assert "​" not in result
    assert result == "Раздва"


def test_rtl_override_stripped() -> None:
    """R-39 review MAJOR 2: U+202E (RTL override) — атака через визуальное перестроение."""
    text = "Купить ‮molokom‬ хлеб"
    result = sanitize_for_display(text)
    assert "‮" not in result
    assert "‬" not in result


def test_bom_stripped() -> None:
    """R-39 review MAJOR 2: U+FEFF (BOM)."""
    text = "﻿Запомнить"
    result = sanitize_for_display(text)
    assert "﻿" not in result
    assert result == "Запомнить"


def test_soft_hyphen_stripped() -> None:
    """R-39 review MAJOR 2: U+00AD (soft-hyphen) — визуально незаметный."""
    soft_hyphen = chr(0x00AD)
    text = f"до{soft_hyphen}лгое слово"
    result = sanitize_for_display(text)
    assert soft_hyphen not in result
    assert result == "долгое слово"


def test_returns_str_for_none_input_or_raises() -> None:
    """None — либо TypeError, либо пустая строка. Выбор реализации."""
    # допускаем оба варианта; главное — не падает в runtime неожиданно
    try:
        result = sanitize_for_display(None)  # type: ignore[arg-type]
    except TypeError:
        return
    assert result == ""
