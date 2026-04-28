"""Тесты `detect_unbacked_claim` — anti-hallucination guard.

Срабатывает когда LLM написал юзеру что что-то сделано (создал
напоминание, добавил рецепт, ...) но соответствующего write-tool
вызова в этот turn не было — значит вранье.

История:
- 2026-04-22 Gemma-4 hallucinated в чате (записывала факт без
  save_core_fact tool).
- 2026-04-28 incident tg_634496616 — LLM писал «Удалила ✅\n— ☐ X»
  с фейковыми checklist items без show_checklist tool.
- 2026-04-29 incident tg_352612382 — LLM ответил «Готово! ⏰ Каждый
  день в 9:00 утра будет напоминание «Принять лекарства».» при
  tools=[]; schedule_reminder НЕ вызван. Этот тест-файл фиксирует
  расширение паттернов — passive-future ("будет напомин") и
  generic-affirmation ("готово") теперь ловятся.
"""

from __future__ import annotations

import pytest

from sreda.services.llm import detect_unbacked_claim


# --------------------------------------------------------------------
# Existing patterns (regression — должны продолжать работать)
# --------------------------------------------------------------------


def test_explicit_save_claim_without_tool_fires() -> None:
    text = "Сохранила рецепт «Борщ» в твою книгу."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_explicit_save_claim_WITH_tool_passes() -> None:
    text = "Сохранила рецепт «Борщ» в твою книгу."
    assert detect_unbacked_claim(
        text, called_tools={"save_recipe"},
    ) is False


def test_added_to_shopping_without_tool_fires() -> None:
    text = "Добавила в список молоко и хлеб."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_passive_no_object_close_no_fire() -> None:
    """«создал» далеко от объектного слова — не fire."""
    text = "Я создал прецедент для этого подхода когда-то давно."
    # "меню" / "рецепт" / etc. отсутствуют — no claim
    assert detect_unbacked_claim(text, called_tools=set()) is False


# --------------------------------------------------------------------
# New patterns (2026-04-29 — incident tg_352612382)
# --------------------------------------------------------------------


def test_gotovo_with_reminder_object_fires() -> None:
    """Точный текст incident'а: «Готово! ⏰ Каждый день в 9:00 утра
    будет напоминание «Принять лекарства»."""
    text = (
        "Готово! ⏰ Каждый день в 9:00 утра будет напоминание "
        "«Принять лекарства».\n\nЧтобы отменить — просто скажи."
    )
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_gotovo_with_reminder_AND_schedule_reminder_tool_passes() -> None:
    """Тот же текст но с правильно вызванным tool — no fire."""
    text = (
        "Готово! ⏰ Каждый день в 9:00 утра будет напоминание "
        "«Принять лекарства»."
    )
    assert detect_unbacked_claim(
        text, called_tools={"schedule_reminder"},
    ) is False


def test_budet_napomin_passive_future_fires() -> None:
    """Passive-future — «будет напоминание» без явного «я создал»."""
    text = "Каждый понедельник в 17:00 будет напоминание про кружок."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_napomnyu_first_person_promise_fires() -> None:
    """«Напомню тебе завтра в 9» без tool = тоже галлюцинация."""
    text = "Напомню тебе завтра в 9 утра принять лекарства."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_gotovo_alone_without_object_no_fire() -> None:
    """«Готово» без объекта-носителя (рецепт/напомин/...) — НЕ fire,
    чтобы не ловить benign acknowledgements."""
    text = "Готово, давай продолжим."
    assert detect_unbacked_claim(text, called_tools=set()) is False


def test_gotova_feminine_form_with_object_fires() -> None:
    """Женская форма «Готова» — тоже паттерн."""
    text = "Готова! Запись добавлена в твой список покупок."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_napominaiu_tense_variant_fires() -> None:
    """«Напоминаю тебе каждый день в 9» — present-tense claim."""
    text = "Напоминаю тебе каждый день в 9 утра принимать таблетки."
    assert detect_unbacked_claim(text, called_tools=set()) is True


# --------------------------------------------------------------------
# False-positive guards (важно — слишком широкий matcher = wasted
# LLM iterations, юзер видит дубль "сейчас попробую ещё раз")
# --------------------------------------------------------------------


def test_user_question_about_reminder_no_fire() -> None:
    """LLM расспрашивает юзера про напоминание — это не claim."""
    text = "Хочешь, поставлю напоминание на завтра?"
    # "поставлю" — будущее, не "поставил". Не должно fire.
    assert detect_unbacked_claim(text, called_tools=set()) is False


def test_negation_no_fire_intentional_limitation() -> None:
    """Известное ограничение: текущий simple word-search не
    различает «не поставил» от «поставил». Зафиксировано как known
    limitation. False-positive здесь приемлем — пользователь увидит
    ретрай но не вранье в финальном ответе."""
    text = "Я не поставил напоминание, нужно подтверждение."
    # Note: detector currently fires here. Если в будущем будет
    # NLU-based negation handling — обновим тест.
    assert detect_unbacked_claim(text, called_tools=set()) is True
