"""#338 часть 2: живой confirm - чистые функции (red-before).

БИБЛИЯ (g-075): никаких технических данных юзеру. Сырое «Я поняла как
«schedule_reminder» (title=…, trigger_iso=…)» заменяется на: факты → допустимые
человеческие формулировки (календарный код) → фраза «рта» в персоне → железная
проверка → сбой = человеческий шаблон.

Здесь - фундамент: извлечение фактов, генератор допустимых формулировок,
человеческий шаблон-фолбэк, верификатор фразы рта.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sreda.runtime.confirm_preview import (
    allowed_day_phrases,
    allowed_time_phrases,
    confirm_facts,
    fallback_template,
    generic_action_question,
    verify_confirm_text,
)

_MSK = ZoneInfo("Europe/Moscow")
_NOW = datetime(2026, 8, 17, 10, 0, tzinfo=_MSK)  # «сегодня» = 17 августа
_TRIGGER = "2026-08-18T15:00:00+03:00"            # «завтра в 15:00»


# ── факты из kwargs ──────────────────────────────────────────────────────────

def test_facts_schedule_reminder_338():
    f = confirm_facts("schedule_reminder",
                      {"title": "выписка лекарств", "trigger_iso": _TRIGGER})
    assert f is not None
    assert f.object_title == "выписка лекарств"
    assert f.action == "поставить напоминание"
    assert f.when_local.hour == 15 and f.when_local.day == 18


def test_facts_unknown_tool_none_338():
    """Неизвестный инструмент → фактов нет → сразу фолбэк-путь (generic)."""
    assert confirm_facts("some_new_tool", {"x": 1}) is None


# ── допустимые формулировки (календарный код) ───────────────────────────────

def test_day_phrases_tomorrow_338():
    phrases = allowed_day_phrases(datetime(2026, 8, 18, 15, 0, tzinfo=_MSK), now=_NOW)
    assert "завтра" in phrases
    assert "18 августа" in phrases
    assert "во вторник" in phrases  # 2026-08-18 - вторник


def test_day_phrases_today_338():
    phrases = allowed_day_phrases(datetime(2026, 8, 17, 23, 0, tzinfo=_MSK), now=_NOW)
    assert "сегодня" in phrases
    assert "17 августа" in phrases


def test_time_phrases_afternoon_338():
    phrases = allowed_time_phrases(datetime(2026, 8, 18, 15, 0, tzinfo=_MSK))
    assert "в 15:00" in phrases
    assert "в три часа дня" in phrases  # владелец: «отличная фраза!»


def test_time_phrases_morning_338():
    phrases = allowed_time_phrases(datetime(2026, 8, 18, 9, 0, tzinfo=_MSK))
    assert "в 9:00" in phrases
    assert "в девять утра" in phrases


# ── человеческий шаблон-фолбэк ──────────────────────────────────────────────

def test_fallback_template_human_338():
    f = confirm_facts("schedule_reminder",
                      {"title": "выписка лекарств", "trigger_iso": _TRIGGER})
    text = fallback_template(f, now=_NOW)
    # человеческий: название + русская дата + время + вопрос
    assert "выписка лекарств" in text
    assert "18 августа" in text
    assert "15:00" in text
    assert text.rstrip().endswith("?")
    # БИБЛИЯ: никакой технической начинки
    assert "schedule_reminder" not in text
    assert "trigger_iso" not in text
    assert "T15:00" not in text  # ISO не светится


# ── верификатор фразы «рта» ─────────────────────────────────────────────────

def _f():
    return confirm_facts("schedule_reminder",
                         {"title": "выписка лекарств", "trigger_iso": _TRIGGER})


def test_verify_accepts_live_phrase_338():
    """«Завтра в три часа дня» - допустимая живая фраза (владелец)."""
    ok = verify_confirm_text(
        "Хорошо! Поставлю напоминание «выписка лекарств» завтра в три часа дня - подтверждаешь?",
        _f(), now=_NOW)
    assert ok


def test_verify_accepts_formal_phrase_338():
    ok = verify_confirm_text(
        "Ставлю напоминание «выписка лекарств» 18 августа в 15:00. Подтверждаешь?",
        _f(), now=_NOW)
    assert ok


def test_verify_rejects_wrong_date_338():
    """Рот переврал дату (19-е вместо 18-го) → отказ → фолбэк."""
    ok = verify_confirm_text(
        "Ставлю напоминание «выписка лекарств» 19 августа в 15:00. Подтверждаешь?",
        _f(), now=_NOW)
    assert not ok


def test_verify_rejects_missing_title_338():
    ok = verify_confirm_text(
        "Ставлю напоминание завтра в три часа дня. Подтверждаешь?", _f(), now=_NOW)
    assert not ok


def test_verify_rejects_technical_leak_338():
    """Техническая начинка (имя инструмента/ISO) → отказ, даже если факты верны."""
    ok = verify_confirm_text(
        "schedule_reminder: «выписка лекарств» завтра в 15:00 (2026-08-18T15:00:00+03:00)?",
        _f(), now=_NOW)
    assert not ok


def test_verify_rejects_no_question_338():
    """Без вопроса подтверждения - не договор."""
    ok = verify_confirm_text(
        "Ставлю напоминание «выписка лекарств» завтра в 15:00.", _f(), now=_NOW)
    assert not ok


# ── generic-вопрос для инструментов без факт-рендера ────────────────────────

def test_generic_question_known_action_338():
    q = generic_action_question("add_task", {"title": "позвонить врачу"})
    assert q == "Хочу добавить задачу «позвонить врачу». Подтверждаешь?"


def test_generic_question_items_batch_338():
    q = generic_action_question("add_shopping_items", {"items": ["молоко", "батон"]})
    assert "молоко" in q and "ещё 1" in q and q.endswith("Подтверждаешь?")


def test_generic_question_never_technical_338():
    """БИБЛИЯ: даже для совсем неизвестного инструмента - ни имени, ни аргументов."""
    q = generic_action_question("brand_new_tool_42", {"weird_arg": "abc", "iso": "2026-08-18T15:00:00"})
    assert "brand_new_tool_42" not in q
    assert "weird_arg" not in q
    assert "2026-08-18" not in q
    assert q.rstrip().endswith("?")
