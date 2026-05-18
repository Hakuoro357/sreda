"""R-39: тесты классификатора намерения хода разговора.

Целевые метрики (на калибровочном корпусе Day 5):
- mutation: recall ≥98%, precision ≥95%
- read: recall ≥90%
- chitchat: recall ≥90%

Fall-safe: при двусмысленности между chitchat и mutation выбираем
mutation (лучше зря подготовить инструмент, чем пропустить действие).
"""

from __future__ import annotations

import pytest

from sreda.services.turn_intent_classifier import (
    TurnClassification,
    TurnIntent,
    classify_turn,
)


# ─── Mutation: явные глаголы действия ──────────────────────────────────


def test_postavi_napominanie() -> None:
    r = classify_turn("Поставь напоминание на 9 утра потягать гантели.")
    assert r.intent is TurnIntent.MUTATION


def test_otmeni_napominanie() -> None:
    r = classify_turn("Отмени напоминание про гантели")
    assert r.intent is TurnIntent.MUTATION


def test_dobavi_v_spisok() -> None:
    r = classify_turn("Добавь в список покупок молоко и хлеб")
    assert r.intent is TurnIntent.MUTATION


def test_zapishi_recipe() -> None:
    r = classify_turn("Запиши рецепт борща: свёкла, капуста, картошка")
    assert r.intent is TurnIntent.MUTATION


def test_napomni_v_9() -> None:
    r = classify_turn("Напомни в 9 принять таблетку")
    assert r.intent is TurnIntent.MUTATION


def test_perenesi_na_zavtra() -> None:
    r = classify_turn("Перенеси встречу с врачом на завтра")
    assert r.intent is TurnIntent.MUTATION


def test_otmet_sdelano() -> None:
    r = classify_turn("Отметь что я сделал зарядку")
    assert r.intent is TurnIntent.MUTATION


def test_udali_napominanie() -> None:
    r = classify_turn("Удали напоминание про врача")
    assert r.intent is TurnIntent.MUTATION


def test_zapomni_chto() -> None:
    r = classify_turn("Запомни что мой день рождения 5 мая")
    assert r.intent is TurnIntent.MUTATION


def test_kupi_imperative() -> None:
    r = classify_turn("Купи молоко завтра")
    assert r.intent is TurnIntent.MUTATION


def test_kuplyu_future_form() -> None:
    """R-39 review MINOR: «куплю» — 1л будущего времени, другая основа («купл-»)."""
    r = classify_turn("Куплю молоко завтра, напомнишь?")
    assert r.intent is TurnIntent.MUTATION


def test_sohrani_recipe() -> None:
    r = classify_turn("Сохрани этот рецепт в книгу")
    assert r.intent is TurnIntent.MUTATION


# ─── Read: запросы данных ──────────────────────────────────────────────


def test_chto_u_menya_v_spiske() -> None:
    r = classify_turn("Что у меня в списке покупок?")
    assert r.intent is TurnIntent.READ


def test_pokazhi_napominaniya() -> None:
    r = classify_turn("Покажи мои напоминания на сегодня")
    assert r.intent is TurnIntent.READ


def test_chto_zaplanirovano() -> None:
    r = classify_turn("Что у меня запланировано на завтра?")
    assert r.intent is TurnIntent.READ


def test_kogda_vstrecha() -> None:
    r = classify_turn("Когда у меня встреча с врачом?")
    assert r.intent is TurnIntent.READ


def test_skolko_zadach() -> None:
    r = classify_turn("Сколько задач на сегодня?")
    assert r.intent is TurnIntent.READ


def test_est_li_napominanie() -> None:
    r = classify_turn("Есть ли у меня напоминание на завтра?")
    assert r.intent is TurnIntent.READ


# ─── Chitchat: болтовня ────────────────────────────────────────────────


def test_kak_dela() -> None:
    r = classify_turn("Как дела?")
    assert r.intent is TurnIntent.CHITCHAT


def test_privet() -> None:
    r = classify_turn("Привет!")
    assert r.intent is TurnIntent.CHITCHAT


def test_spasibo() -> None:
    r = classify_turn("Спасибо за помощь")
    assert r.intent is TurnIntent.CHITCHAT


def test_dobroe_utro() -> None:
    r = classify_turn("Доброе утро!")
    assert r.intent is TurnIntent.CHITCHAT


def test_ok_alone() -> None:
    r = classify_turn("ок")
    assert r.intent is TurnIntent.CHITCHAT


def test_chto_dumaesh() -> None:
    r = classify_turn("Что думаешь?")
    assert r.intent is TurnIntent.CHITCHAT


def test_nyet_alone() -> None:
    """Одиночное «нет» — болтовня (отказ), не correction."""
    r = classify_turn("нет")
    assert r.intent is TurnIntent.CHITCHAT


# ─── Correction-turn → mutation (fall-safe) ────────────────────────────


def test_correction_with_time_no_verb() -> None:
    """«нет, не на 2 а на 14» — correction + time mention → mutation."""
    r = classify_turn("Нет, не на 2 а на 14")
    assert r.intent is TurnIntent.MUTATION


def test_correction_negation_with_replacement() -> None:
    """«Не правильно, на 14:00» — correction + time → mutation."""
    r = classify_turn("Не правильно, на 14:00")
    assert r.intent is TurnIntent.MUTATION


def test_kati_full_correction() -> None:
    """Главный регрессионный тест: точный Кати-кейс."""
    r = classify_turn("Нет, неправильное, поставь на 14:00 разбудить Катю")
    assert r.intent is TurnIntent.MUTATION


def test_oshibka_with_time() -> None:
    r = classify_turn("Ошибка, на 14:00")
    assert r.intent is TurnIntent.MUTATION


# ─── Uncertain — без явных маркеров ────────────────────────────────────


def test_pure_time_without_verb() -> None:
    """«в 14:00» — ни verb, ни correction → uncertain."""
    r = classify_turn("в 14:00")
    assert r.intent is TurnIntent.UNCERTAIN


def test_correction_without_time() -> None:
    """«Не правильно» — correction без replacement → uncertain."""
    r = classify_turn("Не правильно")
    assert r.intent is TurnIntent.UNCERTAIN


def test_empty_text_returns_chitchat() -> None:
    r = classify_turn("")
    assert r.intent is TurnIntent.CHITCHAT


def test_whitespace_returns_chitchat() -> None:
    r = classify_turn("   \n  ")
    assert r.intent is TurnIntent.CHITCHAT


# ─── Confidence ────────────────────────────────────────────────────────


def test_mutation_high_confidence_with_verb() -> None:
    r = classify_turn("Поставь напоминание")
    assert r.confidence >= 0.9


def test_correction_medium_confidence() -> None:
    r = classify_turn("Нет, не на 2 а на 14")
    assert 0.7 <= r.confidence < 0.95


def test_uncertain_low_confidence() -> None:
    r = classify_turn("в 14:00")
    assert r.confidence < 0.6


# ─── Reasons trace ─────────────────────────────────────────────────────


def test_reasons_trace_mutation_verb() -> None:
    r = classify_turn("Поставь напоминание")
    assert any("mutation_verb" in reason for reason in r.reasons)


def test_reasons_trace_correction_and_time() -> None:
    r = classify_turn("Нет, не на 2 а на 14")
    assert any("correction" in reason for reason in r.reasons)
    assert any("time_mention" in reason for reason in r.reasons)


def test_reasons_trace_read() -> None:
    r = classify_turn("Покажи напоминания")
    assert any("read" in reason for reason in r.reasons)


def test_reasons_trace_chitchat() -> None:
    r = classify_turn("Привет!")
    assert any("chitchat" in reason for reason in r.reasons)


# ─── Возвращаемый тип ──────────────────────────────────────────────────


def test_return_type_is_classification() -> None:
    r = classify_turn("Привет")
    assert isinstance(r, TurnClassification)
    assert isinstance(r.intent, TurnIntent)
    assert isinstance(r.confidence, float)
    assert isinstance(r.reasons, list)
