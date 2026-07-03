"""#285 Фаза B срез B1: калибровка сигнальных детекторов против корпусов.

Кодирует `plans/285-signal-corpora-v0.md` — red-кейсы принятого плана. Провал теста = дрейф
детектора от корпуса (тот же класс, что калибровка Фазы 0). High-precision-инвариант write-сигнала
(false-positive = молчаливая запись) проверяется явными негативами-смолтоком.
"""

from __future__ import annotations

import pytest

from sreda.runtime.react_signals import (
    declarative_memory_signal,
    read_cue_domains,
    write_command_signal,
)


# ───────── write_command_signal: позитивы (ярус (а) обязан сработать) ─────────
@pytest.mark.parametrize("text", [
    "добавь молоко",
    "добавь молоко в покупки",
    "сохрани рецепт борща",
    "запиши это",
    "запиши, что я обещал позвонить",
    "запланируй встречу на завтра в 10",
    "внеси поход к врачу в план",
    "отметь молоко купленным",
    "вычеркни молоко",
    "поставь напоминание про смесь",
    "напомни позвонить маме",
    "запомни что я не ем глютен",
    "удали напоминание",
    "удали задачу про хлеб",
    "отмени задачу",
    "перенеси встречу на пятницу",
    "создай список покупок",
    "купи молока",
])
def test_write_command_positive(text):
    assert write_command_signal(text) is True, text


# ───────── write_command_signal: негативы (смолток/факт/near-miss — НЕ сигнал) ─────────
@pytest.mark.parametrize("text", [
    "как дела?",
    "что нового?",
    "как жизнь?",
    "как ты?",
    "доброе утро",
    "спасибо, умница",
    "расскажи анекдот",
    "кто написал Войну и мир?",
    "что в списке?",           # read, не write
    "перескажи напоминания",   # read, не write
    "не удалось открыть дверь",   # паразит «удал»
    "мы удалились со встречи",     # паразит «удал»
    "поставщик задерживает",       # паразит «постав»
    "меня зовут на дачу в выходные",  # near-miss декларации (не команда)
])
def test_write_command_negative(text):
    assert write_command_signal(text) is False, text


# ───────── declarative_memory_signal ─────────
@pytest.mark.parametrize("text", [
    "меня зовут Таня",
    "Меня зовут Таня",
    "живу в Москве",
    "Живу в Москве",
    "у меня двое детей",
    "у меня трое сыновей",
    "моего мужа зовут Ваня",
    "мою дочь зовут Аня",
    "моё имя — Борис",
])
def test_declarative_positive(text):
    assert declarative_memory_signal(text) is True, text


@pytest.mark.parametrize("text", [
    "меня зовут на дачу в выходные",   # near-miss: имя не с заглавной
    "меня зовут обедать",
    "живу надеждой",
    "живу как в сказке",
    "у меня двое суток на решение",
    "как дела?",
    "добавь молоко",                   # команда, не декларация
])
def test_declarative_negative(text):
    assert declarative_memory_signal(text) is False, text


# ───────── read_cue_domains: bounded маппер ─────────
@pytest.mark.parametrize("text,expected", [
    ("перескажи напоминания", {"reminders"}),
    ("что с задачами?", {"tasks"}),
    ("что в списке?", {"checklists", "shopping"}),
    ("покажи дела", {"checklists", "shopping"}),
    ("как меня зовут?", {"memory"}),
    ("помнишь, что я говорила про садик?", {"memory"}),
    ("что у меня записано про врача", {"memory"}),
    ("покажи меню на неделю", {"menu"}),
    ("какой рецепт борща", {"recipes"}),
])
def test_read_cue_positive(text, expected):
    assert set(read_cue_domains(text)) == expected, text


@pytest.mark.parametrize("text", [
    "как дела?",          # идиома — НЕ own-data read (ключевой анти-регресс роутера)
    "как жизнь?",
    "что нового?",
    "доброе утро",
    "как ты сегодня?",
    "сколько варить яйцо?",   # фактовый — baseline web, не own-data
])
def test_read_cue_idiom_and_fact_empty(text):
    assert read_cue_domains(text) == frozenset(), text


def test_read_cue_bounded_not_crossdomain():
    """«перескажи напоминания» → РОВНО reminders, НЕ memory/checklists (bounded, пилляр 3)."""
    d = read_cue_domains("перескажи напоминания")
    assert d == frozenset({"reminders"})
    assert "memory" not in d and "checklists" not in d
