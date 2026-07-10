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


# ── ч.2б: живой «рот» в _generic_confirm_wrap (флаг SREDA_CONFIRM_VOICE) ────

def _wrap_and_capture(monkeypatch, *, voice_on, mouth_reply=None, mouth_raises=False):
    """Прогнать _generic_confirm_wrap с мок-ртом; вернуть показанный confirm-текст."""
    from langchain_core.tools import StructuredTool
    from sreda.runtime import react_loop

    seen = {}
    inner = StructuredTool.from_function(
        func=lambda title="", trigger_iso="": "ok",
        name="schedule_reminder", description="d")
    monkeypatch.setattr(react_loop, "interrupt",
                        lambda payload: seen.update(payload) or "нет")
    monkeypatch.setattr(react_loop, "_confirm_voice_enabled", lambda: voice_on)
    if voice_on:
        class _Resp:
            content = mouth_reply or ""

        def _fake_invoke(llm, msgs, timeout_seconds=0):
            if mouth_raises:
                raise TimeoutError("рот завис")
            return _Resp()
        monkeypatch.setattr(react_loop, "invoke_with_per_call_timeout", _fake_invoke)
        import sreda.services.llm as _llm_mod
        monkeypatch.setattr(_llm_mod, "get_chat_llm", lambda provider=None: object())
    react_loop._generic_confirm_wrap(inner).invoke(
        {"title": "выписка лекарств", "trigger_iso": _TRIGGER})
    return seen["confirm"]


def test_wrap_voice_off_uses_template_338(monkeypatch):
    """Флаг OFF (дефолт) → человеческий шаблон, рот не зовётся."""
    q = _wrap_and_capture(monkeypatch, voice_on=False)
    assert q == "Ставлю напоминание «выписка лекарств» 18 августа в 15:00. Подтверждаешь?"


def test_wrap_voice_valid_live_phrase_338(monkeypatch):
    """Флаг ON + рот дал валидную живую фразу → она и уходит юзеру."""
    live = "Хорошо! Поставлю напоминание «выписка лекарств» 18 августа в три часа дня - подтверждаешь?"
    q = _wrap_and_capture(monkeypatch, voice_on=True, mouth_reply=live)
    assert q == live


def test_wrap_voice_lying_mouth_falls_back_338(monkeypatch):
    """Рот переврал дату (19-е) → верификатор режет → точный шаблон."""
    lying = "Поставлю напоминание «выписка лекарств» 19 августа в 15:00 - подтверждаешь?"
    q = _wrap_and_capture(monkeypatch, voice_on=True, mouth_reply=lying)
    assert "18 августа" in q and "19 августа" not in q


def test_wrap_voice_mouth_down_falls_back_338(monkeypatch):
    """Рот упал/завис → шаблон, ход не падает."""
    q = _wrap_and_capture(monkeypatch, voice_on=True, mouth_raises=True)
    assert "18 августа в 15:00" in q and q.rstrip().endswith("?")


# ── R1-фиксы: adversarial-кейсы верификатора (Claude M3/M4, Codex high M5, medium M4) ──

def test_verify_rejects_poslezavtra_substring_338():
    """R1 MAJOR-3: «послезавтра» при факте «завтра» - подстрочный обход убит."""
    ok = verify_confirm_text(
        "Поставлю напоминание «выписка лекарств» послезавтра в 15:00. Подтверждаешь?",
        _f(), now=_NOW)
    assert not ok


def test_verify_rejects_wrong_month_338():
    """R1 high M5: «18 сентября» при факте 18 августа - месяц сверяется."""
    ok = verify_confirm_text(
        "Ставлю напоминание «выписка лекарств» 18 сентября в 15:00. Подтверждаешь?",
        _f(), now=_NOW)
    assert not ok


def test_verify_rejects_wrong_word_hour_338():
    """R1 high M5: правильное 15:00 + враньё «в четыре вечера» - словесные часы ловятся."""
    ok = verify_confirm_text(
        "Ставлю напоминание «выписка лекарств» завтра в 15:00, то есть в четыре вечера. Подтверждаешь?",
        _f(), now=_NOW)
    assert not ok


def test_verify_rejects_wrong_action_338():
    """R1 medium M4: «Отменяю» не проходит договором «поставить»."""
    ok = verify_confirm_text(
        "Отменяю «выписка лекарств» завтра в 15:00. Подтверждаешь?", _f(), now=_NOW)
    assert not ok


def test_verify_rejects_foreign_number_338():
    """R1 high M5: постороннее число («и ещё 42») - класс «ID 42»."""
    ok = verify_confirm_text(
        "Ставлю напоминание «выписка лекарств» завтра в 15:00 и ещё 42. Подтверждаешь?",
        _f(), now=_NOW)
    assert not ok


def test_verify_requires_quoted_title_338():
    """R1 high M5: название обязано быть в «кавычках» (дословный объект договора)."""
    ok = verify_confirm_text(
        "Ставлю напоминание выписка лекарств завтра в 15:00. Подтверждаешь?",
        _f(), now=_NOW)
    assert not ok


def test_verify_accepts_latin_title_338():
    """R1 все три MINOR: легитимная латиница в названии («Zoom с командой») больше
    НЕ выключает живой голос - title исключается из tech-скана."""
    f = confirm_facts("schedule_reminder",
                      {"title": "Zoom с командой", "trigger_iso": _TRIGGER})
    ok = verify_confirm_text(
        "Ставлю напоминание «Zoom с командой» завтра в 15:00. Подтверждаешь?",
        f, now=_NOW)
    assert ok


# ── R1 оба Codex: повтор - часть договора ───────────────────────────────────

def test_facts_and_template_carry_recurrence_338():
    f = confirm_facts("schedule_reminder",
                      {"title": "пить воду", "trigger_iso": _TRIGGER,
                       "recurrence_rule": "FREQ=HOURLY;COUNT=5"})
    assert f is not None and f.recurrence_human == "каждый час, всего 5 раз"
    text = fallback_template(f, now=_NOW)
    assert "каждый час, всего 5 раз" in text


def test_verify_requires_recurrence_mention_338():
    """Повтор в фактах есть, во фразе нет → враньё (юзер подтвердил бы разовое)."""
    f = confirm_facts("schedule_reminder",
                      {"title": "пить воду", "trigger_iso": _TRIGGER,
                       "recurrence_rule": "FREQ=HOURLY"})
    ok = verify_confirm_text(
        "Ставлю напоминание «пить воду» завтра в 15:00. Подтверждаешь?", f, now=_NOW)
    assert not ok


def test_verify_rejects_phantom_recurrence_338():
    """Повтора в фактах НЕТ, фраза говорит «каждый…» → враньё."""
    ok = verify_confirm_text(
        "Ставлю напоминание «выписка лекарств» завтра в 15:00, повтор каждый день. Подтверждаешь?",
        _f(), now=_NOW)
    assert not ok


def test_unparseable_rrule_no_facts_338():
    """R1 high M2: RRULE не очеловечивается (BYDAY-экзотика) → facts None →
    generic-вопрос (не рискуем переврать договор)."""
    f = confirm_facts("schedule_reminder",
                      {"title": "кружок", "trigger_iso": _TRIGGER,
                       "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU,TH"})
    assert f is None
