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


def test_completion_marker_with_shopping_object_fires() -> None:
    """Round 2 (2026-04-29): «✅ В списке покупок» без verb'а из старого
    списка. Раньше пропускалось, теперь ловится через completion-marker
    («✅») + object («в список»)."""
    text = "✅ В список покупок добавлены молоко и хлеб."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_completion_marker_sdelano_with_reminder_object_fires() -> None:
    text = "Сделано — напоминание на 9 утра каждый день стоит."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_completion_marker_zafiksirovano_with_recipe_object_fires() -> None:
    text = "Зафиксировано: рецепт борща в книге."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_completion_marker_alone_without_object_no_fire() -> None:
    """«Сделано» без domain-object'а — не fire (могут быть legit
    contexts типа «дело сделано, переходим дальше» в общем диалоге)."""
    text = "Сделано! Идём дальше."
    assert detect_unbacked_claim(text, called_tools=set()) is False


def test_completion_marker_with_correct_tool_passes() -> None:
    """«✅ В список покупок» + add_shopping_items вызван — no fire."""
    text = "✅ Список покупок обновлён, молоко и хлеб добавлены."
    assert detect_unbacked_claim(
        text, called_tools={"add_shopping_items"},
    ) is False


def test_negation_no_fire_intentional_limitation() -> None:
    """Известное ограничение: текущий simple word-search не
    различает «не поставил» от «поставил». Зафиксировано как known
    limitation. False-positive здесь приемлем — пользователь увидит
    ретрай но не вранье в финальном ответе."""
    text = "Я не поставил напоминание, нужно подтверждение."
    # Note: detector currently fires here. Если в будущем будет
    # NLU-based negation handling — обновим тест.
    assert detect_unbacked_claim(text, called_tools=set()) is True


# --------------------------------------------------------------------
# 2026-05-08 incident — wrong-tool hallucination (Codex CRITICAL)
# --------------------------------------------------------------------
# Юзер 755682022 продиктовал список покупок («Купить кабачок,
# морковь и фасоль консервированную»). LLM корректно вызвал
# add_shopping_items, но в финальном тексте написал:
#   «Записала в план кроя на пятницу ✅
#    📋 Тенцель шампань: Простыня 240×260, Пододеяльник ...»
#
# Никакого нового кроя в БД не появилось. detect_unbacked_claim
# пропустил это — потому что called_tools пересекалось с
# _WRITE_TOOL_NAMES (add_shopping_items был вызван), и логика
# "any write tool → ack OK" возвращала False.
#
# Bug fix: claim про крой/тенцель/cutting_plan ≠ acknowledgement
# add_shopping_items. Нужна category-aware проверка.


def test_2026_05_08_cutting_plan_claim_with_shopping_tool_fires() -> None:
    """Reproduces the incident: LLM called shopping tool but
    text claims cutting-plan action. Different category — must fire."""
    text = (
        "Записала в план кроя на пятницу ✅\n\n"
        "📋 Тенцель шампань:\n"
        "— Простыня 240×260\n"
        "— Пододеяльник 190×210 на молнии\n"
    )
    # Boris's case: shopping.add was actually called this turn.
    assert (
        detect_unbacked_claim(text, called_tools={"add_shopping_items"})
        is True
    ), (
        "Should detect cutting-plan hallucination even though "
        "add_shopping_items was called — different category"
    )


def test_cutting_plan_claim_without_any_tool_fires() -> None:
    """Cutting plan tool не существует — любая claim про крой
    всегда unbacked, никакой write-tool не делает её правдой."""
    text = "Добавила в план кроя на понедельник тенцель аквамарин."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_shopping_add_with_correct_shopping_text_passes() -> None:
    """Regression guard: правильный flow не должен false-positive."""
    text = (
        "Добавила в покупки: кабачок, морковь, фасоль консервированная ✅"
    )
    assert (
        detect_unbacked_claim(text, called_tools={"add_shopping_items"})
        is False
    )


def test_recipe_claim_with_shopping_tool_fires() -> None:
    """Cross-category: LLM сказала «сохранила рецепт» но вызвала
    только add_shopping_items. Сейчас false-pass из-за loose check."""
    text = "Сохранила рецепт борща в твою книгу ✅"
    assert (
        detect_unbacked_claim(text, called_tools={"add_shopping_items"})
        is True
    )


def test_reminder_claim_with_shopping_tool_fires() -> None:
    """Cross-category: claim про напоминание, вызван shopping."""
    text = "Поставила напоминание на завтра в 9 утра ✅"
    assert (
        detect_unbacked_claim(text, called_tools={"add_shopping_items"})
        is True
    )


# --------------------------------------------------------------------
# Codex r3 fixes (2026-05-08)
# --------------------------------------------------------------------


def test_multi_occurrence_second_claim_unbacked_fires() -> None:
    """Codex r3 MAJOR #1: LLM пишет 2 verb'а в одном тексте — первый
    backed (shopping), второй unbacked (cutting). Раньше детектор
    останавливался на первом успешном match'е и пропускал второй."""
    text = (
        "Добавила в покупки кабачок ✅\n"
        "Также добавила в план кроя на пятницу: тенцель шампань."
    )
    # Shopping tool was called (covers first claim) — но cutting не tool.
    assert (
        detect_unbacked_claim(text, called_tools={"add_shopping_items"})
        is True
    ), "Second occurrence (cutting plan) must still trigger unbacked"


def test_substring_raskroi_does_not_false_fire() -> None:
    """Codex r3 MAJOR #2: «раскрой» (cooking term — раскрой теста)
    не должен false-fire через bare 'крой' substring. Patterns
    tightened до specific phrases."""
    # Realistic legit context — recipe instructions mentioning раскрой.
    text = (
        "Сохранила рецепт пирога. Совет: раскрой теста сделай "
        "тонким, иначе не пропечётся."
    )
    # save_recipe was called → recipe claim backed → should NOT fire.
    assert (
        detect_unbacked_claim(text, called_tools={"save_recipe"})
        is False
    ), "'раскрой теста' must not trigger cutting-plan rule"


def test_substring_pokroi_does_not_false_fire() -> None:
    """«Покрой» (одежды) — также cooking/sewing term, не должен
    matching на bare 'крой'."""
    text = (
        "Записала рецепт. По покрою торта совет: режь радиально."
    )
    assert (
        detect_unbacked_claim(text, called_tools={"save_recipe"})
        is False
    )
