"""vex#171 Шаг 1: анти-галлюцинационное правило #9 в react ``_system_prompt``.

Факты реального мира (места/адреса/организации/ссылки/цены/свежее) → web_search/
fetch_url; называть ТОЛЬКО конкретику из результата инструмента; НЕ конструировать
URL/адрес/название; не нашёл — честно «не нашла». Регрессионный target — инцидент
2026-06-18 (tenant_max_40921122): Фредди выдумал «парк им. Крупкина» + фейк-адрес +
фейк-ссылку, не вызвав веб.

Это presence-тест react-промпта (``_system_prompt``) — НЕ путать с
test_anti_confabulation_prompt.py (тот про легаси-core ``build_system_prompt``).
Проверяем по СМЫСЛОВЫМ якорям (не по дословным длинным фразам) — устойчиво к
безобидным правкам стиля, но ловит исчезновение сути правила. Якоря lowercase'нуты,
чтобы не ломаться от регистра (в промпте НЕ/ТОЛЬКО/СНАЧАЛА капсом).
"""

from __future__ import annotations

from sreda.runtime.react_loop import _system_prompt


def _sp() -> str:
    return _system_prompt("2030-01-01 (Вторник)").lower()


def test_rule9_present_and_forces_web_tools():
    # правила №9 (факты) и №10 (исключения) присутствуют, форсятся ОБА веб-инструмента
    sp = _sp()
    assert "9." in sp
    assert "10." in sp
    assert "web_search" in sp
    assert "fetch_url" in sp


def test_rule9_recommendation_is_factual_not_opinion():
    # «посоветуй/подбери место» — это факт, а НЕ мнение (ровно инцидент)
    sp = _sp()
    assert "посоветуй" in sp or "подбери" in sp
    assert "не мнение" in sp


def test_rule9_output_oriented_trigger():
    # триггер по СОДЕРЖИМОМУ ОТВЕТА, а не по формулировке запроса (Codex high R2 MAJOR)
    sp = _sp()
    assert "если твой ответ" in sp
    assert "какой бы ни была формулировка" in sp


def test_rule9_grounding_only_from_tool_output():
    # называть только конкретику, реально присутствующую в результате инструмента
    sp = _sp()
    assert "в результате инструмента" in sp


def test_rule9_never_construct_even_for_user_named_object():
    # не конструировать/угадывать URL/адрес даже если объект назвал пользователь
    # (в инциденте была фейк-ссылка; Codex high R2 MAJOR — сузить исключение)
    sp = _sp()
    assert "не конструируй" in sp
    assert "url" in sp
    assert "адрес" in sp
    assert "достраивать" in sp  # имя от юзера повторить можно, адрес/ссылку — нет


def test_rule9_honest_unknown_after_search():
    # после поиска не нашёл — честно «не нашла», не выдавать догадку за факт
    sp = _sp()
    assert "не нашла" in sp
    assert "догадку за факт" in sp


def test_rule10_carveouts_weather_and_user_data():
    # исключения: погода → get_weather (НЕ web_search); данные юзера → read-инструменты.
    # Усилено vs R2: проверяем сам carve-out, а не только наличие get_weather (все три R2).
    sp = _sp()
    assert "исключения из правила 9" in sp
    assert "get_weather" in sp
    assert "recall_memory" in sp  # пользовательская память — своим инструментом
    # «память модели» и «пользовательская память» разведены: запрет «из головы»
    assert "из головы" in sp


def test_rule9_grounding_no_invented_details():
    # Шаг 3-lite: числа/адреса/география из выдачи, не досочинять, не сливать сущности
    sp = _sp()
    assert "из выдачи" in sp
    assert "не досочиняй" in sp
    assert "не сливай разные объекты" in sp


def test_no_false_done_word_on_info_answer():
    # Шаг 3-lite: «записала/сохранила/...» только при реальном write-инструменте,
    # на справку/поиск/совет так не говорить (ложное «записала» — правило #7)
    sp = _sp()
    assert "изменила что-то инструментом" in sp
    # #178 расширено: + «погоду» и явный запрет завершать словом «Готово» на справке
    assert "на справку, поиск, погоду или совет так" in sp


def test_rule10_mixed_answer_external_facts_still_rule9():
    # смешанный ответ (данные юзера + внешний факт) — внешнее всё равно по правилу 9
    # (Codex medium R3 MAJOR — шов #9/#10 на смешанных запросах)
    sp = _sp()
    assert "если в том же ответе" in sp
    assert "всё равно действует правило 9" in sp
