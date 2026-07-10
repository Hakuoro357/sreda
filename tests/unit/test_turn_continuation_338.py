"""#338 часть 1: страховка (кандидат-confirm B2b-2) - только на ПЕРВОЕ сообщение
входа в ход (решение владельца 2026-07-10).

Прод-инцидент (user_tg_755682022): Среда сама спросила «Во сколько поставить
напоминание?» (обычным текстом, финал хода с schedule_reminder), человек ответил
«В 15» - и получил кандидат-confirm с сырым «schedule_reminder (title=…,
trigger_iso=…)». Глупо: ответ на НАШ ЖЕ вопрос.

Механизм: «незакрытый ход» = прошлый ход агента закончился ВОПРОСОМ (финальная
реплика с «?») - тогда области инструментов того хода наследуются в allowed_write
текущего (продолжение, не вход). Новая команда в ДРУГУЮ область = выход = страховка.
Паттерн - обобщение #319 sticky-by-use; отличие: гейт сильнее (агент сам запросил
продолжение вопросом), поэтому наследование переживает и безглагольный ответ
(«В 15»), и голую команду без домена («Поставь»).
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime.react_loop import _prev_open_domains
from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import route_domains


def _hist_incident():
    """История инцидента: ход с schedule_reminder, финал - вопрос."""
    return [
        HumanMessage(content="Поставь напоминание на 19 августа выписка лекарства в 15 "
                             "и напоминание на 18 августа что 19 августа в 15 выписка лекарст"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="ok:scheduled:rem_1:2026-08-19T12:00:00Z",
                    name="schedule_reminder", tool_call_id="t1"),
        AIMessage(content="Ставлю напоминание: 19 августа в 15:00 - «выписка лекарства» "
                          "(установлено). Для напоминания на 18 августа нужно уточнить "
                          "время. Во сколько поставить его?"),
    ]


# ── _prev_open_domains: детект незакрытого хода ─────────────────────────────

def test_prev_open_domains_incident_history_338():
    """Финал хода - вопрос + исполнялся schedule_reminder → reminders открыт."""
    assert "reminders" in _prev_open_domains(_hist_incident())


def test_prev_open_domains_closed_turn_338():
    """Финал БЕЗ вопроса (ход закрыт) → наследовать нечего."""
    hist = _hist_incident()
    hist[-1] = AIMessage(content="Готово, напоминание поставлено.")
    assert _prev_open_domains(hist) == set()


def test_prev_open_domains_question_without_tools_338():
    """Вопрос был, но инструментов в ходе не было → областей нет."""
    hist = [HumanMessage(content="привет"),
            AIMessage(content="Привет! Чем помочь?")]
    assert _prev_open_domains(hist) == set()


def test_prev_open_domains_empty_history_338():
    assert _prev_open_domains([]) == set()


# ── policy: наследование в allowed_write ────────────────────────────────────

def test_policy_continuation_v15_incident_338():
    """«В 15» при открытом reminders-ходе → reminders в ярусе (а), БЕЗ кандидата."""
    pol = compute_unified_policy(
        "В 15", route_domains("В 15"), prev_open_domains=frozenset({"reminders"}))
    assert "reminders" in pol["allowed_write"]


def test_policy_continuation_bare_postav_338():
    """Голое «Поставь» (команда без домена) при открытом ходе → продолжение,
    reminders наследуется (агент сам запросил продолжение вопросом)."""
    pol = compute_unified_policy(
        "Поставь", route_domains("Поставь"), prev_open_domains=frozenset({"reminders"}))
    assert "reminders" in pol["allowed_write"]


def test_policy_new_command_other_domain_exits_turn_338():
    """Выход из хода: новая команда в ДРУГУЮ область («добавь молоко в покупки»)
    при открытом reminders-ходе → reminders НЕ наследуется (страховка как обычно)."""
    text = "добавь молоко в покупки"
    pol = compute_unified_policy(
        text, route_domains(text), prev_open_domains=frozenset({"reminders"}))
    assert "reminders" not in pol["allowed_write"]


def test_policy_no_prev_no_inheritance_338():
    """Нет открытого хода → «В 15» ничего не получает (прежний контракт
    «нет домена → кандидат» цел)."""
    pol = compute_unified_policy("В 15", route_domains("В 15"))
    assert pol["allowed_write"] == []


def test_policy_continuation_observable_in_signals_338():
    """Наблюдаемость канарейки: применённое наследование видно в signals."""
    pol = compute_unified_policy(
        "В 15", route_domains("В 15"), prev_open_domains=frozenset({"reminders"}))
    assert pol["signals"].get("turn_continuation") == ["reminders"]


# ── R1-фиксы наследования (Claude M1/M2/M6, Codex high M3/M4, medium M3/M7) ──

def test_prev_open_meta_and_unknown_names_safe_338():
    """R1 MAJOR-1 (все три): ask_human/need_family/галлюцинированное имя в истории
    не роняют детект (раньше KeyError валил ВЕСЬ unified в legacy fail-open)."""
    hist = [
        HumanMessage(content="поставь напоминание про лекарства"),
        AIMessage(content="", tool_calls=[{"name": "need_family", "args": {}, "id": "t1"}]),
        ToolMessage(content="ok", name="need_family", tool_call_id="t1"),
        AIMessage(content="", tool_calls=[{"name": "ask_human", "args": {}, "id": "t2"}]),
        ToolMessage(content="во сколько?", name="ask_human", tool_call_id="t2"),
        AIMessage(content="", tool_calls=[{"name": "set_alarm", "args": {}, "id": "t3"}]),
        ToolMessage(content="ok", name="set_alarm", tool_call_id="t3"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t4"}]),
        ToolMessage(content="ok:scheduled:rem_1:x", name="schedule_reminder", tool_call_id="t4"),
        AIMessage(content="Во сколько поставить второе напоминание?"),
    ]
    assert _prev_open_domains(hist) == {"reminders"}


def test_prev_open_read_tool_gives_no_write_grant_338():
    """R1 консенсус трёх: read-инструмент (list_tasks) + вопрос НЕ открывает write -
    «Вот задачи. Что-нибудь по ним?» не право писать без страховки."""
    hist = [
        HumanMessage(content="покажи задачи"),
        AIMessage(content="", tool_calls=[{"name": "list_tasks", "args": {}, "id": "t1"}]),
        ToolMessage(content="1. позвонить врачу", name="list_tasks", tool_call_id="t1"),
        AIMessage(content="Вот твои задачи. Что-то поменять по ним?"),
    ]
    assert _prev_open_domains(hist) == set()


def test_prev_open_closing_phrase_not_continuation_338():
    """R1 MAJOR-6: вежливая закрывашка «Что-нибудь ещё?» - ход ЗАКРЫТ."""
    hist = _hist_incident()
    hist[-1] = AIMessage(content="Готово, поставила! Что-нибудь ещё?")
    assert _prev_open_domains(hist) == set()


def test_prev_open_declined_candidate_not_continuation_338():
    """R1 MAJOR-6: отклонённый кандидат («Хорошо, не делаю») ход не открывает."""
    hist = [
        HumanMessage(content="поставь напоминание про воду"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="Хорошо, не делаю.", name="schedule_reminder", tool_call_id="t1"),
        AIMessage(content="Не ставлю. Что-то скорректировать?"),
    ]
    assert _prev_open_domains(hist) == set()


def test_prev_open_time_not_specified_opens_338():
    """time_not_specified - УТОЧНЯЮЩИЙ исход (ход 2 инцидента: «Поставь» →
    time_not_specified → «Во сколько?») - обязан открывать ход."""
    hist = [
        HumanMessage(content="Поставь"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="уточни время", name="schedule_reminder", tool_call_id="t1",
                    artifact={"result_kind": "time_not_specified"}),
        AIMessage(content="Во сколько точно поставить напоминание?"),
    ]
    assert "reminders" in _prev_open_domains(hist)


def test_prev_open_blocked_outcome_not_continuation_338():
    """domain_blocked-исход область не открывает (инструмент не работал)."""
    hist = [
        HumanMessage(content="сделай что-нибудь"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="раздел недоступен", name="schedule_reminder", tool_call_id="t1",
                    artifact={"result_kind": "domain_blocked"}),
        AIMessage(content="Уточни, что именно нужно?"),
    ]
    assert _prev_open_domains(hist) == set()


def test_prev_open_list_content_aimessage_338():
    """R1 MINOR-10: блочный content (list) - текст извлекается, вопрос детектится."""
    hist = _hist_incident()
    hist[-1] = AIMessage(content=[{"type": "text", "text": "Во сколько поставить его?"}])
    assert "reminders" in _prev_open_domains(hist)
