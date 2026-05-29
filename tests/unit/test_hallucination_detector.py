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


# --------------------------------------------------------------------
# 2026-05-08 r2 (Boris feedback "только крой?") — broader no-tool
# self-asserting verbs. Ловят hallucinations про несуществующие tools.
# --------------------------------------------------------------------


def test_sewing_action_sshila_fires() -> None:
    """«Сшила» — швея-action; tool не существует."""
    text = "Сшила тебе простыню из тенцель шампань ✅"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_sewing_action_vykroila_fires() -> None:
    text = "Выкроила лайм 140×200 на простыню ✅"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_payment_oplatila_schet_fires() -> None:
    text = "Оплатила счёт за электричество, 1240 ₽ ✅"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_payment_perevela_dengi_fires() -> None:
    text = "Перевела деньги на твой счёт мамы — 5000 ₽."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_taxi_zakazala_fires() -> None:
    text = "Заказала такси Яндекс на 18:00 ✅"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_doctor_zapisala_fires() -> None:
    text = "Записала к врачу 14 мая в 10:00 ✅"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_email_otpravila_pismo_fires() -> None:
    text = "Отправила письмо учительнице о пропуске."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_image_gen_sgenerirovala_fires() -> None:
    text = "Сгенерировала картинку с котом — приложила."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_calendar_postavila_fires() -> None:
    text = "Поставила в календарь встречу с клиентом на 16:00."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_govservices_podala_zayavlenie_fires() -> None:
    text = "Подала заявление на пособие через Госуслуги ✅"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_phone_pozvonila_v_organization_fires() -> None:
    """«Позвонила в социальный фонд» — completed call claim."""
    text = "Позвонила в социальный фонд, твою заявку принимают ✅"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_pozvonit_infinitive_does_not_fire() -> None:
    """«Поставила напоминание позвонить» — infinitive, не claim
    о том что Среда позвонила. Reminder backed → ОК."""
    text = (
        "Поставила напоминание позвонить в социальный фонд "
        "сегодня в 11 утра ✅"
    )
    assert (
        detect_unbacked_claim(text, called_tools={"schedule_reminder"})
        is False
    )


# --------------------------------------------------------------------
# Pass 4 (2026-05-11): READ-side citation claims
# Production incident user_tg_755682022 16:18-16:33 — модель цитировала
# «у тебя в чек-листе X», «оттуда взяла», прогноз погоды +14°C без
# соответствующих read-tool calls. См. Codex+Xiaomi consensus review.
# --------------------------------------------------------------------


def test_read_claim_checklist_cite_without_tool_fires() -> None:
    """Production case 16:33: «висят как незавершённые в чек-листе»
    без list_checklists/show_checklist — должно сработать."""
    text = (
        "Они висят как незавершённые пункты в чек-листе «План кроя». "
        "Видимо, ты их уже сделала, просто не закрыла."
    )
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_read_claim_checklist_cite_with_tool_passes() -> None:
    """Тот же текст, но show_checklist был реально вызван — OK."""
    text = (
        "Они висят как незавершённые пункты в чек-листе «План кроя»."
    )
    assert (
        detect_unbacked_claim(text, called_tools={"show_checklist"})
        is False
    )


def test_read_claim_ottuda_vzyala_without_tool_fires() -> None:
    """Production case 16:33: «оттуда и взяла» — direct false source cite."""
    text = (
        "В памяти помечены как выполненные, но в чек-листе висят как "
        "pending. Оттуда и взяла."
    )
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_read_claim_u_tebya_v_spiske_without_tool_fires() -> None:
    """«у тебя в списке X» без list_shopping — fabricated cite."""
    text = "У тебя в списке покупок только молоко, хлеб и яйца."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_read_claim_u_tebya_v_spiske_with_tool_passes() -> None:
    """Та же фраза с list_shopping в called_tools — OK."""
    text = "У тебя в списке покупок только молоко, хлеб и яйца."
    assert (
        detect_unbacked_claim(text, called_tools={"list_shopping"})
        is False
    )


def test_read_claim_v_pamiati_without_tool_fires() -> None:
    """«В моей памяти записано...» без recall_memory — claim о памяти."""
    text = "В моей памяти записано, что ты планировала закончить крой к пятнице."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_read_claim_question_does_not_fire() -> None:
    """«У тебя в списке есть мясо?» — это вопрос юзера/боту, не cite.
    Должно НЕ срабатывать (skip question context)."""
    text = "У тебя в списке покупок есть мясо?"
    # Verb-like phrase «у тебя в» + question mark in close window → skip.
    # NOTE: bot не должен задавать такой вопрос (он сам должен проверить),
    # но если задаёт — это не lying claim, false-positive здесь хуже.
    assert detect_unbacked_claim(text, called_tools=set()) is False


def test_read_claim_po_planu_without_tool_fires() -> None:
    """«По плану у тебя три раскроя» без list_checklists/recall_memory."""
    text = "По плану кроя у тебя три позиции остались: простыня 220×240, наволочки 50×70."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_read_claim_v_knige_recipes_without_tool_fires() -> None:
    """«В книге рецептов у тебя борщ и омлет» без search_recipes."""
    text = "Нашла в твоей книге рецептов борщ и омлет — хочешь приготовить?"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_read_claim_v_knige_recipes_with_tool_passes() -> None:
    text = "Нашла в твоей книге рецептов борщ и омлет."
    assert (
        detect_unbacked_claim(text, called_tools={"search_recipes"})
        is False
    )


# --------------------------------------------------------------------
# R-12 (sreda#26) regression — intra-sentence object window + question-offer skip
# Production incident 2026-05-12 09:41 (user_max_40921122):
# bot reply «Записала на завтра в 13:00 — тренировка с гирей 💪\n\nНужно
# напоминание перед тренировкой?» с add_task tool вызванным → Pass 2 captured
# «напомина» из второго предложения → false fire → safe_ack replaced
# legitimate reply.
# --------------------------------------------------------------------


def test_r12_task_create_with_reminder_offer_question_no_fire() -> None:
    """Boris prod case 2026-05-12 09:41: add_task created + follow-up
    offer «Нужно напоминание?» в SLEDующем предложении не должно
    fire'ить (intra-sentence cut)."""
    text = (
        "Записала на завтра в 13:00 — тренировка с гирей 💪\n\n"
        "Нужно напоминание перед тренировкой?"
    )
    assert (
        detect_unbacked_claim(text, called_tools={"add_task"})
        is False
    )


def test_r12_recipes_save_with_menu_question_no_fire() -> None:
    """Variant: рецепты сохранены + follow-up «Составить меню?» —
    «в меню» в next sentence не должно ловить."""
    text = (
        "Сохранила 3 рецепта в книгу.\n\n"
        "Хочешь, составлю меню на неделю на основе них?"
    )
    assert (
        detect_unbacked_claim(text, called_tools={"save_recipes_batch"})
        is False
    )


def test_r12_question_offer_in_same_sentence_no_fire() -> None:
    """Single-sentence claim + offer-question с «?» близко к object —
    Layer B skip должен сработать (object «напомин» + ? в <15 chars).

    Codex r2 #5 fix: prior test had unused `text` variable + relied на
    sentence boundary вместо Layer B. Этот тест явно проверяет
    question-skip path (нет sentence boundary между object и `?`)."""
    # task создан → add_task backed. Bot offer «Напомнить тебе позже?» —
    # «напомнить» (≈ «напомин») followed by «?» очень близко. Должно skip.
    text = "Я записала задачу. Напомнить?"
    # Window для verb «записала»: до `?` (boundary) = «я записала задачу. напомнить»
    # Inside iterate: objects — «задач» (backed), «напомин» (would be unbacked
    # without skip). After object «напомин» + 1 char до `?` → skip ✓.
    assert (
        detect_unbacked_claim(text, called_tools={"add_task"}) is False
    )


def test_r12_cross_sentence_genuine_claim_still_fires() -> None:
    """Don't over-correct: если в РАЗНЫХ предложениях два РЕАЛЬНЫХ
    claim'а, и хоть один unbacked — должно fire'ить."""
    # Verb1 + object1 backed, verb2 + object2 unbacked.
    text = "Сохранила рецепт. Поставила напоминание на завтра."
    # «Поставила напоминание» = self-claiming reminder, no schedule_reminder tool.
    # «Сохранила рецепт» — verb «сохранила» в первом предложении, object «рецепт»
    # в том же предложении → category=recipe → save_recipes_batch in tools → OK.
    # «Поставила напомин...» во втором предложении: verb «поставила напомин»
    # match'ит _CLAIM_VERBS «поставила напомин» → reminder claim BUT
    # called_tools = {save_recipes_batch} doesn't include reminder tools → FIRE.
    assert (
        detect_unbacked_claim(text, called_tools={"save_recipes_batch"})
        is True
    )


def test_r12_legitimate_offer_no_false_fire() -> None:
    """Bot offers next action: «Список обновлён. Хочешь рецепт?» —
    «рецепт» в offer-question. tools=[shopping]. Раньше fired (false),
    после R-12 fix не fire'ит."""
    text = "Список покупок обновлён.\n\nХочешь, найду рецепт борща?"
    assert (
        detect_unbacked_claim(
            text,
            called_tools={"add_shopping_items"},
        )
        is False
    )


# --------------------------------------------------------------------
# R-12 r2 (Codex MAJOR review): iterate-all-objects + tighten
# question-skip + drop `.` boundary
# --------------------------------------------------------------------


def test_r12_r2_split_claim_recipe_backed_reminder_unbacked_fires() -> None:
    """Codex r2 MAJOR #1: «Сохранила рецепт! Также поставила
    напоминание на завтра.» — recipe-claim backed, reminder claim
    unbacked → должно fire.

    R-12 r1 firstly returned object «рецепт» (recipe), backed by
    save_recipes_batch, skip → reminder never checked. r2 iterates
    ALL objects."""
    text = "Сохранила рецепт! Также поставила напоминание на завтра."
    assert (
        detect_unbacked_claim(text, called_tools={"save_recipes_batch"})
        is True
    )


def test_r12_r2_completion_marker_cross_sentence_fires() -> None:
    """Codex r2 MAJOR #3: «Сохранила. Рецепт в книге.» без
    save_recipes_batch tool — раньше fired (legit single claim).
    R-12 r1 с `.` boundary cut'нул бы window до «сохранила.» (object
    не в окне) → false negative. r2 dropped `.` → fires correctly."""
    text = "Сохранила. Рецепт в книге."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_r12_r2_confirmation_question_after_claim_fires() -> None:
    """Codex r2 MAJOR #2: «Записала задачу на четверг, верно?» БЕЗ
    add_task tool — это claim + confirmation, не offer-question.
    `?` через 17+ chars после «задачу» → не должно skip Layer B."""
    text = "Записала задачу на четверг, верно?"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_r12_r2_dates_in_text_dont_break_window() -> None:
    """Codex r2 minor: «.» в датах/абсхр не должны cut window.
    `.` больше не boundary → safe."""
    # «12.05» в тексте — не должно резать window.
    text = "Записала на 12.05 задачу — встреча с врачом."
    # verb «записала» + object «задач» в SAME sentence (no `?`, `\n\n`)
    # but old `.` boundary fired at «12.» → window cut → no object → no fire.
    # New: `.` not boundary → window includes «задач» → fire (no add_task).
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_r12_r2_xiaomi_verb_at_end_of_sentence_fires() -> None:
    """Xiaomi review #2: verb at the end of sentence «Рецепт борща
    сохранила.» — verb is at end, object before."""
    text = "Рецепт борща сохранила."
    # verb «сохранила» idx ~12, len 9. Pre-window (-40) от idx 12 = 0
    # → covers «рецепт борща ». Object «рецепт» found → category=recipe.
    # No save_recipes tool called → fire.
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_r12_r2_distant_mention_does_not_false_fire() -> None:
    """R-12 r2 closest-object protects: «Сохранила рецепт борща — теперь
    у тебя 5 рецептов в книге.» с save_recipes_batch tool.
    verb «сохранила» end at ~9. Objects: «рецепт» idx 11 (dist 2),
    «в книг» idx ~50 (dist 41). Closest = «рецепт», backed → no fire.
    Без closest-object iterate-all нашёл бы «в книг» (тоже recipe, тоже backed).
    Этот тест protect от случая когда distant object был БЫ unbacked."""
    # Variant с unbacked distant object: «Сохранила рецепт — потом
    # составлю меню!» с save_recipes_batch.
    # verb «сохранила» end at 9. «рецепт» idx ~11 (dist 2),
    # «меню» idx ~40 (dist 31). Closest = «рецепт» backed → no fire.
    # Без closest: iterate-all нашёл бы «меню» unbacked → false fire.
    # NB: «составлю» — future verb (Pass 1c) с object «меню» далеко
    # от verb «составлю». Pass 1c с question-context check: «можно
    # составлю»? нет, просто «потом составлю» — declarative future
    # claim, BUT no menu tool → ловится Pass 1c.
    # Используем variant без future verb для clean isolation:
    text = "Сохранила рецепт борща, теперь у тебя есть выбор в меню."
    # verb «сохранила» idx 0 len 9. closest object: «рецепт» idx 10
    # (dist 1) vs «в меню» idx ~45 (dist 36). «рецепт» wins, backed.
    # No future verb → no Pass 1c fire.
    assert (
        detect_unbacked_claim(text, called_tools={"save_recipes_batch"})
        is False
    )


def test_r12_r3_question_skip_bounded_to_window() -> None:
    """Xiaomi r2 MAJOR fix regression: question-skip must search в
    `window`, не в global `low`. «Сохранила рецепт\\n\\nМеню?» —
    реальный claim первого предложения. Window cuts на `\\n\\n`,
    «рецепт» в window. Раньше post_obj глобально находил `?`
    через 6 chars после object → ошибочно skip. После r3 fix:
    post_obj в window = «\\n\\n» (рецепт идёт перед `\\n\\n`) →
    `?` НЕ в local window → не skip → fire ✓."""
    text = "Сохранила рецепт\n\nМеню?"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_r12_r3_paragraph_break_isolates_claim() -> None:
    """Xiaomi r2 minor #4: positive-fire test с `\\n\\n`.
    «Сохранила рецепт\\n\\nПоставила напоминание.» — оба предложения
    реальные claims. Window для verb «сохранила» cuts на `\\n\\n` —
    «напомина» НЕ в window. Но verb «поставила напомин» в outer loop
    triggers own iteration → ловит «напомина» (unbacked) → fire ✓.
    Защищает от accidental cross-paragraph closest-object grab."""
    text = "Сохранила рецепт\n\nПоставила напоминание."
    assert detect_unbacked_claim(text, called_tools=set()) is True


# --------------------------------------------------------------------
# R-12 r3 (Codex r2 MAJOR): iterate-all-with-proximity + drop question-skip
# --------------------------------------------------------------------


def test_r12_r3_codex_multi_object_single_verb_fires() -> None:
    """Codex r2 MAJOR #1: «Записала задачу и напоминание на завтра»
    с add_task tool — single verb claims TWO side-effects. add_task
    backed («задач»), но reminder («напомина») НЕ backed → fire.

    R-12 r2 closest-object выбирал «задач» как ближайший, пропускал
    «напомина». r3 iterate-all + proximity 30c ловит оба."""
    text = "Записала задачу и напоминание на завтра."
    assert (
        detect_unbacked_claim(text, called_tools={"add_task"})
        is True
    )


def test_r12_r3_codex_past_claim_with_confirmation_question_fires() -> None:
    """Codex r2 MAJOR #2: «Записала задачу, ок?» БЕЗ add_task.
    Past-tense claim сделан, `?` — confirmation. Question-skip
    больше не маскирует Pass 2 fire."""
    text = "Записала задачу, ок?"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_r12_r3_codex_past_claim_with_offer_question_fires() -> None:
    """Codex r2 MAJOR #2: «Записала задачу. Напомнить?» БЕЗ add_task.
    Past-tense «записала задачу» = claim, «Напомнить?» = offer на
    next action. Без question-skip Pass 2 правильно ловит unbacked
    «задач»."""
    text = "Записала задачу. Напомнить?"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_r12_r3_mention_beyond_proximity_no_fire() -> None:
    """R-12 r3 proximity threshold: distant mention НЕ fire-ит.
    «Сохранила рецепт борща и омлет — теперь у тебя 5 рецептов в
    книге, можно составить меню!» с save_recipes_batch tool.
    «рецепт» dist 1 ✓ (backed). «меню» dist 50+ > 30 → mention,
    ignore. No fire."""
    text = (
        "Сохранила рецепт борща и омлет — теперь у тебя 5 рецептов "
        "в книге, можно составить меню!"
    )
    assert (
        detect_unbacked_claim(text, called_tools={"save_recipes_batch"})
        is False
    )


# --------------------------------------------------------------------
# R-12 r4 (Codex r3 MAJOR): per-object offer-question detection +
# iterate-all-occurrences
# --------------------------------------------------------------------


def test_r12_r4_codex_offer_question_with_modal_no_fire() -> None:
    """Codex r3 MAJOR #1: «Записала задачу, нужно напоминание?» с
    add_task. «задачу» backed → ok. «напомина» — modal «нужно» before
    + `?` after → offer-question, skip. No fire ✓."""
    text = "Записала задачу, нужно напоминание?"
    assert (
        detect_unbacked_claim(text, called_tools={"add_task"})
        is False
    )


def test_r12_r4_offer_question_hochesh_modal_no_fire() -> None:
    """«Сохранила рецепт, хочешь добавить ингредиенты в покупки?»
    с save_recipes_batch — «хочешь» before «в покупк» + `?` after."""
    text = "Сохранила рецепт, хочешь добавить ингредиенты в покупки?"
    assert (
        detect_unbacked_claim(
            text, called_tools={"save_recipes_batch"},
        )
        is False
    )


def test_r12_r4_offer_question_mojet_modal_no_fire() -> None:
    """«Готово ✅ Может составить меню?» — «может» before «меню» + `?`."""
    text = "Готово ✅ Может составить меню?"
    # «готово» в _CLAIM_VERBS. «меню» found но preceded by «может» (modal).
    # Should NOT fire — это offer-question.
    assert detect_unbacked_claim(text, called_tools=set()) is False


def test_r12_r4_codex_iterate_all_occurrences_fires() -> None:
    """Codex r3 MAJOR #2 regression: ALL occurrences of obj iterated.
    Если first occurrence распознан как mention (dist > 30), но second
    occurrence proximate (dist < 30) — должно fire."""
    # Construct: verb + distant FIRST «задач» occurrence (mention),
    # then close SECOND «задач» (real claim, unbacked).
    # «у тебя 5 задач в книге, я также записала задачу на завтра»
    # - verb «записала» idx ~36 (estimated)
    # - First «задач» occurrence at idx 10 (in «5 задач в книге») —
    #   dist from verb_end ~ |10 - (36+8)| = 34 > 30 → mention.
    # - Second «задач» occurrence at idx ~50 («задачу на завтра») —
    #   dist from verb_end ~ |50 - 44| = 6 < 30 → claim.
    # called_tools=set() → task NOT backed → fire.
    text = "у тебя 5 задач в книге, я также записала задачу на завтра"
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_r12_r4_confirmation_no_modal_still_fires() -> None:
    """Защита от over-correction: «Записала задачу, верно?» —
    confirmation, не offer. БЕЗ modal trigger → не skip. Should fire
    if no add_task tool."""
    text = "Записала задачу, верно?"
    assert detect_unbacked_claim(text, called_tools=set()) is True


# --------------------------------------------------------------------
# R-12 r5 (Codex r4 MAJOR): unbounded post-object `?` search +
# expanded modal list
# --------------------------------------------------------------------


def test_r12_r5_long_offer_question_no_fire() -> None:
    """Codex r4 MAJOR #1: реальный bot reply «Записала задачу, нужно
    напоминание перед тренировкой?» — object stem «напомина» at idx ~24,
    `?` через 35+ chars. R4 15c after окно miss'ило. R5 ищет в rest of
    window — sentence-bounded, безопасно."""
    text = "Записала задачу, нужно напоминание перед тренировкой?"
    assert (
        detect_unbacked_claim(text, called_tools={"add_task"})
        is False
    )


def test_r12_r5_modal_davay_no_fire() -> None:
    """Codex r4 MAJOR #2: «Записала задачу, давай поставлю напоминание?»
    с add_task — «давай» modal перед «напомин» + `?` after → skip."""
    text = "Записала задачу, давай поставлю напоминание?"
    assert (
        detect_unbacked_claim(text, called_tools={"add_task"})
        is False
    )


def test_r12_r5_modal_mozhem_no_fire() -> None:
    """Codex r4 MAJOR #2: «Готово. Можем составить меню?» — modal
    «можем» перед «меню» + `?`."""
    text = "Готово. Можем составить меню?"
    assert detect_unbacked_claim(text, called_tools=set()) is False


def test_r12_r5_modal_mozhet_with_comma_no_fire() -> None:
    """Xiaomi r4 minor #1: «Готово, может, добавить напоминание?» —
    «может,» с comma вместо space. R4 «может ` requires trailing space —
    miss'ило. R5 убрали trailing space → catches «может,»."""
    text = "Готово, может, добавить напоминание?"
    assert detect_unbacked_claim(text, called_tools=set()) is False


def test_r12_r6_modal_mogu_no_fire() -> None:
    """Codex r5 MAJOR: «Записала задачу, могу поставить напоминание?»
    с add_task — «могу» modal перед «напомин» + `?` after → skip."""
    text = "Записала задачу, могу поставить напоминание?"
    assert (
        detect_unbacked_claim(text, called_tools={"add_task"})
        is False
    )


# --------------------------------------------------------------------
# 2026-05-29 (vex-assistant#79) — show_checklist render false-positive.
# Production incident user_tg_755682022 [SREDA P1]: юзер попросил
# показать чек-лист «Дела по машине». show_checklist отрендерил
# выполненные пункты с глифами ☑ («Выполнено: ☑ Получить справку …»).
# Глифы ☑/☐/✗ одновременно входят в _CLAIM_VERBS И в _OBJECT_TO_CATEGORY
# (→ "checklist", expected_tools = только WRITE), а show_checklist —
# READ-tool, его в write-наборе нет → detect_unbacked_claim вернул True
# → safety_net ЗАМЕНИЛ корректный чек-лист на safe-ack (handlers.py).
# Фикс: когда вызван checklist-render READ-tool, глифы — это backed
# display-маркеры, нейтрализуем их до claim-скана. Word-based claim'ы
# («добавила», «пункт») остаются, чтобы реальная write-галлюцинация
# с одним лишь read-tool по-прежнему ловилась.
# --------------------------------------------------------------------


def test_checklist_render_done_items_with_show_checklist_no_fire() -> None:
    """Точный кейс инцидента: рендер выполненных пунктов через
    show_checklist не должен fire'ить."""
    text = (
        "Дела по машине:\n"
        "☐ Поменять масло\n"
        "☐ Записаться на ТО\n\n"
        "Выполнено:\n"
        "☑ Принести маме пакетики с пшеном\n"
        "☑ Отнести маме пакетики с пшеном\n"
        "☑ Получить справку"
    )
    assert (
        detect_unbacked_claim(text, called_tools={"show_checklist"})
        is False
    )


def test_single_done_checkbox_with_show_checklist_no_fire() -> None:
    text = "☑ Получить справку"
    assert (
        detect_unbacked_claim(text, called_tools={"show_checklist"})
        is False
    )


def test_checklist_render_done_items_without_tool_still_fires() -> None:
    """True-positive сохраняется: те же ☑-пункты, но БЕЗ show_checklist
    (LLM выдумала рендер) — должно fire'ить (incident tg_634496616)."""
    text = (
        "Удалила ✅\n"
        "☑ Получить справку\n"
        "☐ Поменять масло"
    )
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_word_write_claim_not_masked_by_show_checklist() -> None:
    """Фикс НЕ должен маскировать word-based write-claim: бот сказал
    «Добавила пункт» но вызвал только READ show_checklist (без
    add_checklist_items) — это реальная галлюцинация, fire."""
    text = "Добавила пункт «купить антифриз» в чек-лист."
    assert (
        detect_unbacked_claim(text, called_tools={"show_checklist"})
        is True
    )


def test_word_state_marker_vyfolneno_with_show_checklist_no_fire() -> None:
    """Codex r1 MAJOR #2: словесный путь. «В чек-листе ... Выполнено:
    ☑ X» — verb «выполнено» (state marker) + объект «чек-лист» в одном
    окне (single \\n не режет _intra_sentence_window). При вызванном
    show_checklist это backed display, не должно fire'ить."""
    text = (
        "В чек-листе «Дела по машине»:\n"
        "Выполнено:\n"
        "☑ Получить справку"
    )
    assert (
        detect_unbacked_claim(text, called_tools={"show_checklist"})
        is False
    )


def test_shopping_checkmark_claim_with_show_checklist_still_fires() -> None:
    """Codex r1 MAJOR #1 (false-negative guard): «✅ В список покупок:
    молоко» при вызванном ТОЛЬКО show_checklist. Объект — shopping, не
    checklist, поэтому display-backing не применяется → реальный
    shopping-claim без add_shopping_items должен fire'ить."""
    text = "✅ В список покупок: молоко"
    assert (
        detect_unbacked_claim(text, called_tools={"show_checklist"})
        is True
    )


def test_write_claim_checklist_with_only_show_checklist_fires() -> None:
    """Write-verb + checklist объект + только READ show_checklist (без
    create_checklist/add_checklist_items) — мутация не backed read'ом."""
    text = "Создала чек-лист и добавила туда пункт."
    assert (
        detect_unbacked_claim(text, called_tools={"show_checklist"})
        is True
    )


def test_checkmark_emoji_checklist_claim_with_show_checklist_fires() -> None:
    """Codex r2 high MAJOR: «✅ В чек-лист: молоко» — ✅ это generic
    write-affirmation, show_checklist его НЕ рендерит. Не должен быть
    display-backed → реальный checklist write-claim без write-tool fire."""
    text = "✅ В чек-лист «Дела»: молоко"
    assert (
        detect_unbacked_claim(text, called_tools={"show_checklist"})
        is True
    )


def test_otmecheno_checklist_claim_with_show_checklist_fires() -> None:
    """Codex r2 high MEDIUM: «Отмечено в чек-листе: X» — «отмечено» это
    пассивный write-ack («[я] отметила»), не tool-rendered state. Не
    backed read'ом → fire (без mark_checklist_item_done)."""
    text = "Отмечено в чек-листе: купить антифриз."
    assert (
        detect_unbacked_claim(text, called_tools={"show_checklist"})
        is True
    )


def test_r12_r5_long_modal_phrasing_no_fire() -> None:
    """Xiaomi r4 minor #3: «Хочешь, чтобы я добавил напоминание?» —
    «хочешь» на расстоянии ~24 chars от «напомин». R4 20c BEFORE окно
    miss'ило. R5 расширили до 30c."""
    text = "Хочешь, чтобы я добавил напоминание?"
    # No claim verb («хочешь» / «добавил» — let's check)
    # «добавил» в _CLAIM_VERBS yes. verb_end ~22. «напомина» at idx ~26.
    # Distance ~4 within proximity. Modal «хочешь» at idx 0, distance to
    # «напомин» idx 26 = 26 chars. 30c window → matches → skip.
    # called_tools=set() — would fire без skip, with skip → no fire.
    assert detect_unbacked_claim(text, called_tools=set()) is False


# --------------------------------------------------------------------
# 2026-05-29 (vex-assistant) — «План кроя» — это чек-лист по имени.
# Production incident tenant_tg_755682022 16:33 UTC (trace
# trace_73c9fe5786b04838):
#   iter.0 add_checklist_items(list_id_or_title="План кроя",
#                              items=["Муслин зелёный широкий…"]) → ok
#   iter.1 AIMessage "Записала в план кроя: Муслин…" (без tool_calls)
#   safety_net fired → category для «план кро» = None → unbacked
#   iter.2 retry-нудж → LLM сделал ВТОРОЙ add_checklist_items
#          (list_id_or_title="Дела по ткани") → дубль "Муслин…"
#          и в "План кроя", и в "Дела по ткани".
#
# Фикс: «план кро» / «план на крой» → category="checklist".
# add_checklist_items теперь backing'ит claim. Bare " крой " / "в крой "
# (швейный процесс) остаются None — для них tool'а нет.
# --------------------------------------------------------------------


def test_2026_05_29_zapisala_v_plan_kroya_with_add_checklist_items_no_fire() -> None:
    """Точный кейс инцидента: «Записала в план кроя: Муслин зелёный
    широкий — расход 1,57 м + 1,57 м.» при вызванном add_checklist_items
    больше НЕ должно fire'ить."""
    text = (
        "✅ Записала в план кроя: Муслин зелёный широкий — расход "
        "1,57 м + 1,57 м."
    )
    assert (
        detect_unbacked_claim(text, called_tools={"add_checklist_items"})
        is False
    ), (
        "add_checklist_items должен backing'ить claim про «план кроя» — "
        "это чек-лист по имени, не отдельная сущность."
    )


def test_2026_05_29_plan_kroya_with_create_checklist_no_fire() -> None:
    """create_checklist тоже валидный backing для «план кроя» claim."""
    text = "Создала чек-лист «План кроя» и записала туда муслин."
    assert (
        detect_unbacked_claim(
            text,
            called_tools={"create_checklist", "add_checklist_items"},
        )
        is False
    )


def test_2026_05_29_plan_kroya_with_shopping_tool_still_fires() -> None:
    """Regression guard: cross-category по-прежнему ловится. Если LLM
    сказал «в план кроя» но вызвал ТОЛЬКО add_shopping_items — это
    реальная wrong-tool галлюцинация (как 2026-05-08 incident)."""
    text = "Записала в план кроя на пятницу ✅: тенцель шампань."
    assert (
        detect_unbacked_claim(
            text, called_tools={"add_shopping_items"},
        )
        is True
    ), (
        "Cross-category: shopping tool НЕ backing'ит checklist claim — "
        "должно остаться unbacked (защищает 2026-05-08 fix)."
    )


def test_2026_05_29_plan_kroya_without_any_tool_still_fires() -> None:
    """Regression guard: claim про «план кроя» при tools=[] — голая
    галлюцинация, должно fire'ить."""
    text = "Добавила в план кроя на понедельник тенцель аквамарин."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_2026_05_29_bare_kroy_still_unbacked() -> None:
    """Bare «в крой» / « крой » (швейный процесс, не имя чек-листа) —
    tool'а нет, остаётся unbacked даже с add_checklist_items."""
    # «в крой на пятницу» — швейная операция, не чек-лист по имени.
    text = "Записала в крой на пятницу муслин зелёный."
    # Даже если LLM на всякий случай вызвала add_checklist_items —
    # claim про швейную операцию remains unbacked (None category).
    assert (
        detect_unbacked_claim(
            text, called_tools={"add_checklist_items"},
        )
        is True
    )
