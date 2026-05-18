"""R-39: тесты замка живой фразы.

Замок — последний слой защиты от confab-класса бага. Три правила:
1. Длина ≤ 120
2. Нет claim verbs (фактические действия — они в первой строке)
3. Нет новых сущностей (числа/время/имена которых нет в первой строке)
"""

from __future__ import annotations

import pytest

from sreda.agents.live_phrase_lock import (
    LockFail,
    LockPass,
    validate_live_phrase,
)


# ─── Базовое пропускание ─────────────────────────────────────────────


def test_warm_phrase_passes() -> None:
    """Тёплая фраза без действий — пропускаем."""
    first_line = "Поставила ⏰ «Разбудить Катю» на сегодня в 14:00"
    phrase = "Как раз перед обедом разбудишь её 🌞"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockPass)


def test_empty_phrase_passes() -> None:
    """Пустая фраза — допустимо (caller решит)."""
    assert isinstance(validate_live_phrase("", "что-то"), LockPass)


def test_whitespace_only_passes() -> None:
    assert isinstance(validate_live_phrase("   \n  ", "что-то"), LockPass)


def test_emoji_only_passes() -> None:
    """Только эмодзи — это OK."""
    result = validate_live_phrase("🌞", "Поставила на 14:00")
    assert isinstance(result, LockPass)


# ─── Правило 1: длина ────────────────────────────────────────────────


def test_too_long_fails() -> None:
    phrase = "А" * 121
    result = validate_live_phrase(phrase, "first")
    assert isinstance(result, LockFail)
    assert result.reason == "too_long"


def test_exactly_120_passes() -> None:
    phrase = "А" * 120
    result = validate_live_phrase(phrase, "first")
    assert isinstance(result, LockPass)


def test_custom_max_length() -> None:
    result = validate_live_phrase("длинно длинно длинно", "first", max_length=10)
    assert isinstance(result, LockFail)
    assert result.reason == "too_long"


# ─── Правило 2: claim verbs ──────────────────────────────────────────


def test_postavila_claim_verb_fails() -> None:
    """«Поставила» — глагол действия, уже сказан первой строкой."""
    phrase = "Поставила всё как надо!"
    result = validate_live_phrase(phrase, "Поставила на 14:00")
    assert isinstance(result, LockFail)
    assert result.reason == "claim_verb_in_phrase"
    assert result.detail == "поставила"


def test_otmenila_claim_verb_fails() -> None:
    phrase = "Готово, отменила и заодно почистила"
    result = validate_live_phrase(phrase, "Отменила «Х»")
    assert isinstance(result, LockFail)
    assert result.reason == "claim_verb_in_phrase"


def test_sokhranila_claim_verb_fails() -> None:
    phrase = "Сохранила, не забудь приготовить"
    result = validate_live_phrase(phrase, "Сохранила рецепт «Борщ»")
    assert isinstance(result, LockFail)


def test_zapomnila_claim_verb_fails() -> None:
    phrase = "Запомнила, что 5 мая важная дата"
    result = validate_live_phrase(phrase, "Запомнила: день рождения")
    assert isinstance(result, LockFail)


def test_verb_with_negation_still_fails() -> None:
    """«Не поставила» — глагол всё равно claim. Лучше избегать."""
    phrase = "Не поставила, потому что неясно"
    result = validate_live_phrase(phrase, "first")
    assert isinstance(result, LockFail)


def test_neutral_verbs_pass() -> None:
    """«Будет», «пришло», «жди» — нейтральные глаголы, не claim."""
    phrase = "Будет тёплый вечер, не забудь зонтик"
    first_line = "Поставила «Прогулка» на сегодня в 18:00"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockPass)


# ─── Правило 3: новые сущности ───────────────────────────────────────


def test_new_time_in_phrase_fails() -> None:
    """LLM упомянула 16:00, но первая строка про 14:00 — рассинхрон."""
    phrase = "Не забудь — в 16:00 встреча"
    first_line = "Поставила «Разбудить Катю» на 14:00"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockFail)
    assert result.reason == "new_entity_in_phrase"
    assert "16:00" in result.detail


def test_new_number_in_phrase_fails() -> None:
    """Случайное число в фразе — потенциальный confab."""
    phrase = "Кстати, у тебя ещё 7 задач на сегодня"
    first_line = "Поставила «Разбудить» на 14:00"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockFail)
    assert result.reason == "new_entity_in_phrase"


def test_new_proper_name_in_phrase_fails() -> None:
    """Новое имя собственное в фразе — confab."""
    phrase = "Не забудь — Серёжа ждёт звонка"
    first_line = "Поставила «Разбудить Катю» на 14:00"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockFail)
    assert result.reason == "new_entity_in_phrase"


def test_same_time_passes() -> None:
    """Время уже в первой строке — повтор в фразе допустим."""
    phrase = "Как раз к 14:00 будешь готов 🌞"
    first_line = "Поставила «Разбудить Катю» на 14:00"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockPass)


def test_same_proper_name_passes() -> None:
    phrase = "Катя обрадуется"
    first_line = "Поставила «Разбудить Катю» на 14:00"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockPass)


def test_quoted_text_must_match_first_line() -> None:
    """Цитата в фразе («Молоко») должна быть в первой строке."""
    phrase = "Не забудь про «огурцы», тоже важно"
    first_line = "Добавила в список: молоко, хлеб"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockFail)
    assert result.reason == "new_entity_in_phrase"


# ─── Кати-сценарий (главный) ────────────────────────────────────────


def test_kati_good_warm_phrase_passes() -> None:
    """Реалистичная живая фраза для Кати-кейса."""
    first_line = "Поставила ⏰ «Разбудить Катю» на сегодня в 14:00"
    phrase = "Как раз перед обедом 🌞"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockPass)


def test_kati_bad_repeat_action_fails() -> None:
    """LLM зачем-то повторила действие → блок."""
    first_line = "Поставила ⏰ «Разбудить Катю» на сегодня в 14:00"
    phrase = "Поставила всё как нужно, не переживай"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockFail)
    assert result.reason == "claim_verb_in_phrase"


def test_kati_bad_wrong_time_fails() -> None:
    """LLM упомянула неправильное время → блок."""
    first_line = "Поставила ⏰ «Разбудить Катю» на сегодня в 14:00"
    phrase = "Удачи в 16:00 🌞"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockFail)
    assert result.reason == "new_entity_in_phrase"


def test_katok_does_not_match_katyu() -> None:
    """R-39 review MAJOR 2: 4-char stem НЕ должен принять «Каток» за «Катю».

    3-char stem «кат» — false-positive. 4-char stem «като» (Каток) ≠
    «катю» — фраза блокируется как новая сущность.
    """
    phrase = "Пойдём на Каток"
    first_line = "Поставила «Разбудить Катю» на 14:00"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockFail)
    assert result.reason == "new_entity_in_phrase"


def test_katya_matches_katyu_morphologically() -> None:
    """Та же сущность в разных формах должна совпасть (4-char stem)."""
    # Реально в Среде first_line обычно содержит ту же форму что в реплике
    # пользователя, поэтому case «Катя в phrase / Катю в first_line» —
    # маловероятен, но проверим что одинаковая форма работает.
    phrase = "Катюшу обрадует"
    first_line = "Поставила «Разбудить Катюшу» на 14:00"
    result = validate_live_phrase(phrase, first_line)
    assert isinstance(result, LockPass)


def test_long_phrase_with_action_word_short_circuits_on_length() -> None:
    """Длинная фраза проверяется первым правилом (length) до verb check."""
    phrase = "А" * 130 + " поставила"
    result = validate_live_phrase(phrase, "first")
    assert isinstance(result, LockFail)
    assert result.reason == "too_long"
