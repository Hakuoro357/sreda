"""Тесты для медицинских правил privacy_guard (152-ФЗ ст. 10).

Sanitized text должен заменять триггерные мед-слова на плейсхолдеры
[allergy] / [diagnosis], plaintext остаётся в SecureRecord (encrypted
at-rest). LLM получает sanitized — отвечает без разглашения мед-фактов.

Кейсы безопасности — слова, которые НЕ должны триггериться:
  «молочка», «глютен», «мясо» — это ингредиенты, не диагноз.
"""

from __future__ import annotations

import pytest

from sreda.services.privacy_guard import RegexPrivacyGuard


@pytest.fixture
def guard() -> RegexPrivacyGuard:
    return RegexPrivacyGuard()


# ----------------------------- allergy -----------------------------

def test_allergy_basic(guard):
    res = guard.sanitize_text("У Пети аллергия на молоко")
    assert res is not None
    assert "[allergy]" in res.sanitized_text
    assert "аллергия" not in res.sanitized_text.lower()
    assert any(e.entity_type == "allergy" for e in res.entities)


def test_allergy_inflected_forms(guard):
    """Русский — флективный язык, нужно ловить разные формы."""
    for variant in (
        "У Пети аллергии на молоко",
        "У него аллергический ринит",
        "У ребёнка непереносимость лактозы",
    ):
        res = guard.sanitize_text(variant)
        assert res is not None
        # хотя бы один маркер сработал
        assert any(
            e.entity_type == "allergy" for e in res.entities
        ), f"failed on: {variant}"


def test_allergy_does_not_trigger_on_food_words(guard):
    """Безопасные слова из меню (молочка, глютен, мясо) НЕ должны
    помечаться как мед-данные — иначе сломается «составь меню без X»."""
    safe_phrases = [
        "Купи молочки в магазине",
        "Без глютена, пожалуйста",
        "Сделай меню без мяса",
        "Сахар в чай",
    ]
    for phrase in safe_phrases:
        res = guard.sanitize_text(phrase)
        assert res is not None
        assert not any(
            e.entity_type in ("allergy", "diagnosis")
            for e in res.entities
        ), f"unexpected medical match in: {phrase}"


# ----------------------------- diagnosis -----------------------------

def test_diagnosis_basic(guard):
    res = guard.sanitize_text("У него диагноз — бронхит")
    assert res is not None
    assert "[diagnosis]" in res.sanitized_text
    assert any(e.entity_type == "diagnosis" for e in res.entities)


def test_diagnosis_inflected_forms(guard):
    for variant in (
        "У него заболевание сердца",
        "У ребёнка серьёзная болезнь",
        "Точного диагноза пока нет",
    ):
        res = guard.sanitize_text(variant)
        assert res is not None
        assert any(
            e.entity_type == "diagnosis" for e in res.entities
        ), f"failed on: {variant}"


# ----------------------------- composition -----------------------------

def test_other_rules_still_work(guard):
    """Добавление мед-правил не должно ломать существующие правила."""
    res = guard.sanitize_text("email mom@example.com и тел +79991234567")
    assert res is not None
    assert "[email]" in res.sanitized_text
    assert "[phone]" in res.sanitized_text


def test_combined_medical_and_pii(guard):
    res = guard.sanitize_text(
        "У ребёнка аллергия, звонок врачу +79991234567"
    )
    assert res is not None
    assert "[allergy]" in res.sanitized_text
    assert "[phone]" in res.sanitized_text
    types = {e.entity_type for e in res.entities}
    assert "allergy" in types
    assert "phone" in types


def test_original_text_preserved(guard):
    res = guard.sanitize_text("У Пети аллергия")
    assert res is not None
    # original_text — это PII; sanitized идёт в LLM, original — в SecureRecord
    assert res.original_text == "У Пети аллергия"
    assert res.sanitized_text != res.original_text
