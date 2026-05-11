"""Тесты `_is_weather_hallucination` — Layer 4 weather runtime guard.

Срабатывает когда user_text спрашивает про погоду, reply содержит
weather facts (температура / осадки), но get_weather НЕ был вызван
в этом turn'е. Substitute reply на «Не смогла проверить погоду…».

История:
- 2026-05-11 incident user_tg_755682022 16:18 — модель выдала
  «В воскресенье 17 мая в Сходне будет +14°C, без осадков, +18°C
  днём» без get_weather → 6-day forecast выдуман.
"""

from __future__ import annotations

from sreda.runtime.handlers import _is_weather_hallucination


def test_fabricated_forecast_without_tool_fires() -> None:
    """Production case 16:18."""
    user_text = "Какая погода на Сходне 17 числа?"
    reply = (
        "В воскресенье 17 мая в Сходне будет хорошая погода: "
        "☀️ Утро: +14°C, без осадков, 🌤️ Днём: до +18°C."
    )
    assert _is_weather_hallucination(user_text, reply, set()) is True


def test_forecast_with_get_weather_passes() -> None:
    """Тот же reply, но get_weather реально вызван — OK."""
    user_text = "Какая погода на Сходне 17 числа?"
    reply = (
        "В воскресенье 17 мая в Сходне будет хорошая погода: "
        "☀️ Утро: +14°C, без осадков."
    )
    assert (
        _is_weather_hallucination(user_text, reply, {"get_weather"})
        is False
    )


def test_user_not_asking_about_weather_skips() -> None:
    """Юзер спрашивает про рецепт, а в reply случайно «+14°C» (e.g.
    температура для теста) — guard не должен срабатывать."""
    user_text = "Скажи рецепт бисквита."
    reply = "Разогрей духовку до +180°C и испеки 30 минут."
    # «+180°C» матчит regex, но user не спрашивал погоду → skip.
    assert _is_weather_hallucination(user_text, reply, set()) is False


def test_weather_question_no_weather_facts_skips() -> None:
    """Юзер спросил про погоду, но reply не содержит фактов
    (e.g. «сейчас гляну» / «уточни, пожалуйста») — это OK,
    модель честно не выдумывает."""
    user_text = "Какая погода завтра?"
    reply = "Сейчас гляну прогноз — секунду."
    assert _is_weather_hallucination(user_text, reply, set()) is False


def test_weather_facts_bez_osadkov_fires() -> None:
    """Reply содержит «без осадков» — типичный forecast marker."""
    user_text = "Будет ли дождь на выходных?"
    reply = "На выходных без осадков, температура около +12°C."
    assert _is_weather_hallucination(user_text, reply, set()) is True


def test_weather_cyrillic_degree_sign_fires() -> None:
    """Xiaomi часто выдаёт кириллическую «С» в «°С» вместо латинской.
    Regex покрывает оба варианта."""
    user_text = "Прогноз на завтра?"
    reply = "Завтра ожидается +15°С, утром облачно с прояснениями."
    assert _is_weather_hallucination(user_text, reply, set()) is True


def test_weather_negative_temperature_fires() -> None:
    user_text = "Какая температура завтра ночью?"
    reply = "Ночью температура воздуха около -5°C, без осадков."
    assert _is_weather_hallucination(user_text, reply, set()) is True


# --------------------------------------------------------------------
# Impl review fixes (Codex+Xiaomi 2026-05-11)
# --------------------------------------------------------------------


def test_followup_without_weather_keyword_in_user_text_fires_on_multi_match() -> None:
    """Codex #2: user follow-up «А 18-го?» после weather discussion.
    user_text без weather keyword, но reply содержит 2+ weather facts
    → должно сработать (multi-match bypass)."""
    user_text = "А 18-го?"
    reply = (
        "В понедельник 18 мая утром +12°C, днём +17°C, без осадков, "
        "вечером похолодает до +10°C."
    )
    # Multi-match (3+ temperatures) → fire regardless of user_text.
    assert _is_weather_hallucination(user_text, reply, set()) is True


def test_cooking_temperature_single_match_skips() -> None:
    """Xiaomi #1 / false-positive guard. «Разогрей духовку до +180°C» —
    1 weather-pattern match, user НЕ спрашивал погоду → skip."""
    user_text = "Скажи рецепт бисквита."
    reply = "Разогрей духовку до +180°C, испеки 30 минут."
    assert _is_weather_hallucination(user_text, reply, set()) is False


def test_dnem_3_times_does_not_false_fire() -> None:
    """Xiaomi #1: «днём 3 раза проверю» — раньше regex `днём \\+?\\d+`
    ловил bare digit, false-positive. Теперь требует °C/градус рядом."""
    user_text = "Напомни про лекарство."
    reply = "Поставила: днём 3 раза проверять давление, утром натощак."
    assert _is_weather_hallucination(user_text, reply, set()) is False


def test_shopping_specific_match_does_not_accept_list_menu_only() -> None:
    """Codex #1 / Xiaomi #3 регрессия (тест на ordering fix). Бот
    говорит «у тебя в списке покупок молоко и хлеб» — должно требовать
    list_shopping, недостаточно list_menu."""
    from sreda.services.llm import detect_unbacked_claim

    text = "У тебя в списке покупок молоко и хлеб."
    # called_tools = {list_menu} — НЕ должно засчитываться как backed
    # (specific «список покуп» mapping = list_shopping only).
    assert detect_unbacked_claim(text, called_tools={"list_menu"}) is True
    # called_tools = {list_shopping} — OK
    assert (
        detect_unbacked_claim(text, called_tools={"list_shopping"})
        is False
    )


# --------------------------------------------------------------------
# Round 2 impl review fixes (Codex+Xiaomi 2026-05-11 R2)
# --------------------------------------------------------------------


def test_cooking_multi_temperature_does_not_false_fire() -> None:
    """Codex R2 #1 / Xiaomi R2 #1: «нагрей до 220°C, потом понизь
    до 180°C» — 2 temperature matches, user НЕ спрашивал погоду.
    Раньше match_count>=2 bypass'ил user_text gate и был false-positive.
    Теперь требует strong marker (осадки/прогноз/днём+градус)."""
    user_text = "Как испечь хлеб?"
    reply = (
        "Разогрей духовку до +220°C, поставь хлеб, через 10 минут "
        "понизь до +180°C и пеки ещё 30 минут."
    )
    assert _is_weather_hallucination(user_text, reply, set()) is False


def test_forecast_strong_marker_fires_without_user_keyword() -> None:
    """Codex R1 #2 / R2 verification: follow-up «А 18-го?» без
    weather keyword в user_text, но reply содержит strong marker
    («без осадков» / «днём +X°C») → fire."""
    user_text = "А 18-го?"
    reply = "Утром +12°C, днём +17°C, без осадков, вечером похолодает."
    # Strong marker «без осадков» + «днём +17°C» → Path 1 fires.
    assert _is_weather_hallucination(user_text, reply, set()) is True


def test_temperatura_vozduha_strong_marker_fires() -> None:
    """«Температура воздуха около +15°C» — однозначно forecast,
    cooking так не пишут."""
    user_text = "Что там вообще?"  # no weather keyword
    reply = "Температура воздуха около +15°C, ветер юго-западный."
    assert _is_weather_hallucination(user_text, reply, set()) is True


def test_punkt_naznacheniya_does_not_false_fire() -> None:
    """Codex R2 #2: bare «пункт» удалён из _READ_OBJECT_TO_TOOL.
    «Вижу в карте пункт назначения» — раньше fire'ил false-positive."""
    from sreda.services.llm import detect_unbacked_claim

    text = "Вижу в карте пункт назначения на проспекте Мира."
    # verb «вижу в» + object «пункт» (old) → would fire as checklist
    # claim. After fix: no checklist-object → no fire.
    assert detect_unbacked_claim(text, called_tools=set()) is False


def test_chek_list_explicit_still_fires() -> None:
    """Regression: «чек-лист» still triggers без list_checklists."""
    from sreda.services.llm import detect_unbacked_claim

    text = "У тебя в чек-листе три невыполненных задачи."
    assert detect_unbacked_claim(text, called_tools=set()) is True
