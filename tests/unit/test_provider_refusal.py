"""Tests для 12.7 — provider refusal substitution.

Incident tg=634496616 2026-05-03: MiMo content-filter вернул pre-canned
английский «The request was rejected because it was considered high risk»
как content. Без detection юзер видел непонятный английский. Fix:
``_is_provider_refusal`` + ``_is_predominantly_non_russian`` детектят
такие случаи + LLM-output substitute'ится с русским fallback.
"""

from __future__ import annotations

from sreda.runtime.handlers import (
    _is_predominantly_non_russian,
    _is_provider_refusal,
)


# --- _is_provider_refusal ---


def test_refusal_mimo_high_risk():
    assert _is_provider_refusal(
        "The request was rejected because it was considered high risk"
    ) is True


def test_refusal_case_insensitive():
    assert _is_provider_refusal(
        "the REQUEST was REJECTED because it was considered HIGH risk"
    ) is True


def test_refusal_with_surrounding_text():
    """Pattern must match even с дополнительным текстом."""
    assert _is_provider_refusal(
        "Sorry, the request was rejected because it was considered high risk. "
        "Please try a different query."
    ) is True


def test_refusal_other_patterns():
    assert _is_provider_refusal("I cannot fulfill this request.") is True
    assert _is_provider_refusal("I'm sorry, but I can't help with this.") is True


def test_refusal_normal_russian_reply():
    assert _is_provider_refusal(
        "Записала! Завтра в 12:00 — написать стоматологу ✅"
    ) is False


def test_refusal_empty_text():
    assert _is_provider_refusal("") is False
    assert _is_provider_refusal("   ") is False


# --- _is_predominantly_non_russian ---


def test_non_russian_pure_english_long():
    assert _is_predominantly_non_russian(
        "This is a long English-only response without any Russian whatsoever."
    ) is True


def test_non_russian_mostly_russian():
    assert _is_predominantly_non_russian(
        "Привет, это нормальный русский ответ от бота с небольшим EN."
    ) is False


def test_non_russian_short_text_not_flagged():
    """Короткие тексты не детектим — могут быть emoji-only ack'и."""
    assert _is_predominantly_non_russian("Hi") is False
    assert _is_predominantly_non_russian("✅ OK") is False


def test_non_russian_emoji_with_russian():
    assert _is_predominantly_non_russian(
        "🎉 Поздравляю с этим достижением, это здорово!"
    ) is False


def test_non_russian_just_punctuation_long():
    """Длинный текст без русских букв и без alpha — flagged."""
    assert _is_predominantly_non_russian(
        "1234567890 1234567890 1234567890 1234567890"
    ) is True
