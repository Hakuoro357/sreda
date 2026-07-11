"""#338 R6: страховка (кандидат-confirm B2b-2) - только на первое сообщение входа.

Финальная семантика (после CRITICAL R6 Codex medium): наследование области в ярус
(а) - ТОЛЬКО при структурно НЕЗАКРЫТОМ СЛОТЕ прошлого хода: write-инструмент вернул
слот-исход (time_not_specified - «нужно время», allowlist). Успешный ok-исход ход
ЗАКРЫВАЕТ: «Готово, поставила.» → «Я буду у врача завтра в 15» (факт, не команда)
НЕ наследует - иначе тихая мутация. Продолжение = ответ юзера без самостоятельной
темы (нет доменных слов route/read_cues). Текст агента не анализируется вообще.
Инцидент 755682022: «Поставь» → time_not_specified → «Во сколько?» → «В 12:30»
ставит без переспроса; а после закрытого ok-хода уточнение идёт через ЧЕЛОВЕЧЕСКИЙ
кандидат-confirm (1 тап) - осознанная цена безопасности.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime.react_loop import _prev_open_domains
from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import route_domains


def _hist_incident():
    """Открытый ход: schedule_reminder вернул слот-исход time_not_specified
    (ход 2 инцидента: «Поставь» → «нужно время» → «Во сколько?»)."""
    return [
        HumanMessage(content="Поставь"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="уточни время", name="schedule_reminder", tool_call_id="t1",
                    artifact={"result_kind": "time_not_specified"}),
        AIMessage(content="Во сколько точно поставить напоминание?"),
    ]


def _hist_closed_ok():
    """ЗАКРЫТЫЙ ход: write исполнен успешно (ok) - слот закрыт."""
    return [
        HumanMessage(content="Поставь напоминание про лекарства завтра в 15"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="ok:scheduled:rem_1:x", name="schedule_reminder", tool_call_id="t1",
                    artifact={"result_kind": "ok"}),
        AIMessage(content="Готово, поставила!"),
    ]


# ── _prev_open_domains: детект незакрытого хода ─────────────────────────────

def test_prev_open_domains_incident_history_338():
    """Финал хода - вопрос + исполнялся schedule_reminder → reminders открыт."""
    assert "reminders" in _prev_open_domains(_hist_incident())


def test_prev_open_domains_any_final_text_338():
    """R6: финальный текст агента НЕ анализируется - при открытом СЛОТЕ область
    наследуется независимо от формулировки финала."""
    for final in ("Во сколько поставить его?", "Уточни время.", "Что-нибудь ещё?"):
        hist = _hist_incident()
        hist[-1] = AIMessage(content=final)
        assert "reminders" in _prev_open_domains(hist), final


def test_r6_ok_outcome_closes_turn_338():
    """R6 CRITICAL (Codex medium): успешный ok-исход ЗАКРЫВАЕТ ход - «Готово,
    поставила.» → «Я буду у врача завтра в 15» (факт) не наследует write."""
    assert _prev_open_domains(_hist_closed_ok()) == set()
    pol = compute_unified_policy(
        "Я буду у врача завтра в 15", route_domains("Я буду у врача завтра в 15"),
        prev_open_domains=frozenset(_prev_open_domains(_hist_closed_ok())))
    assert pol["allowed_write"] == []


def test_r6_unavailable_does_not_open_338():
    """R6 (оба Codex): unavailable/mode_mismatch и любые non-slot исходы ход не
    открывают (позитивный allowlist вместо blacklist)."""
    for kind in ("unavailable", "mode_mismatch", "ok", "search_limit"):
        hist = _hist_incident()
        hist[2] = ToolMessage(content="x", name="schedule_reminder", tool_call_id="t1",
                              artifact={"result_kind": kind})
        assert _prev_open_domains(hist) == set(), kind


def test_r6_artifact_error_does_not_open_338():
    """R6 medium: artifact result_kind=error (не только status) не открывает."""
    hist = _hist_incident()
    hist[2] = ToolMessage(content="error: сбой", name="schedule_reminder", tool_call_id="t1",
                          artifact={"result_kind": "error"})
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
        ToolMessage(content="уточни время", name="schedule_reminder", tool_call_id="t4",
                    artifact={"result_kind": "time_not_specified"}),
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


def test_r2_multi_write_domain_fail_closed_338():
    """R2 medium: смешанный ход (add_task + schedule_reminder + вопрос) НЕ наследует
    ничего - иначе ошибочный task-write прошёл бы без страховки."""
    hist = [
        HumanMessage(content="добавь задачу и поставь напоминание"),
        AIMessage(content="", tool_calls=[{"name": "add_task", "args": {}, "id": "t1"}]),
        ToolMessage(content="дата?", name="add_task", tool_call_id="t1",
                    artifact={"result_kind": "time_not_specified"}),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t2"}]),
        ToolMessage(content="уточни время", name="schedule_reminder", tool_call_id="t2",
                    artifact={"result_kind": "time_not_specified"}),
        AIMessage(content="Уточни дату задачи и время напоминания?"),
    ]
    assert _prev_open_domains(hist) == set()  # два слот-домена = неоднозначно


def test_r2_read_cue_other_domain_exits_338():
    """R2 Codex high: явный READ-запрос в другую область («покажи задачи» при
    открытом reminders-ходе) - смена темы, наследование закрывается."""
    text = "покажи задачи"
    pol = compute_unified_policy(
        text, route_domains(text), prev_open_domains=frozenset({"reminders"}))
    assert "reminders" not in pol["allowed_write"]


def test_r2_declined_text_freeze_via_real_wrap_338(monkeypatch):
    """R2 Claude MINOR: текст отказа кандидата - через РЕАЛЬНЫЙ wrap (дрейф
    литерала ломал бы фильтр наследования молча)."""
    from langchain_core.tools import StructuredTool
    from sreda.runtime import react_loop

    inner = StructuredTool.from_function(func=lambda title="": "ok",
                                         name="schedule_reminder", description="d")
    monkeypatch.setattr(react_loop, "interrupt", lambda payload: "нет")
    declined = react_loop._generic_confirm_wrap(inner).invoke({"title": "x"})
    hist = [
        HumanMessage(content="поставь напоминание"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content=str(declined), name="schedule_reminder", tool_call_id="t1"),
        AIMessage(content="Не ставлю. Скорректировать что-то?"),
    ]
    assert _prev_open_domains(hist) == set()


def test_r3_read_cue_including_same_domain_exits_338():
    """R3 high: «покажи задачи и напоминания» (read-cue содержит и наследуемый
    домен) - всё равно выход: юзер ушёл смотреть, не отвечает на вопрос."""
    text = "покажи задачи и напоминания"
    pol = compute_unified_policy(
        text, route_domains(text), prev_open_domains=frozenset({"reminders"}))
    assert "reminders" not in pol["allowed_write"]


def test_r5_domain_word_in_answer_is_entry_338():
    """R5: ЛЮБОЕ доменное слово = самостоятельная тема = вход со страховкой -
    включая слот-ответ «напоминание в 15» (осознанный residual: лишний
    ЧЕЛОВЕЧЕСКИЙ confirm - безопасная сторона, юзер обычно отвечает «в 15»)."""
    for text in ("напоминание в 15", "перескажи напоминания", "что с задачами",
                 "добавь молоко в покупки"):
        pol = compute_unified_policy(
            text, route_domains(text), prev_open_domains=frozenset({"reminders"}))
        assert "reminders" not in pol["allowed_write"], text


def test_r5_error_outcome_does_not_open_338():
    """R5 (улов смежного sticky-теста): ошибочный write-исход (status=error или
    result_kind=error) область НЕ открывает - инструмент не отработал."""
    hist = [
        HumanMessage(content="поставь напоминание про воду"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="error: провайдер недоступен", name="schedule_reminder",
                    tool_call_id="t1", status="error"),
        AIMessage(content="Не получилось, попробуем ещё раз. Во сколько поставить?"),
    ]
    assert _prev_open_domains(hist) == set()


def test_r5_memory_is_sticky_jurisdiction_338():
    """R5: memory НЕ наследуется этим механизмом - продолжение memory-серий ведёт
    sticky-by-use #319 (только факт УСПЕШНОЙ записи)."""
    hist = [
        HumanMessage(content="запомни мой вес"),
        AIMessage(content="", tool_calls=[{"name": "save_episode", "args": {}, "id": "t1"}]),
        ToolMessage(content="saved_episode:1", name="save_episode", tool_call_id="t1"),
        AIMessage(content="Записала! Продолжим?"),
    ]
    assert _prev_open_domains(hist) == set()


# ── R6-полировка (Claude NSC + MINOR): атакующий класс и границы окна ────────

def test_r6_bare_imperatives_inherit_destructive_still_confirmed_338():
    """Голые императивы без темы («удали всё», «отмени») наследуют область -
    осмысленно: деструктив в любом случае за своим bespoke-confirm (живая
    проверка R6: cancel_reminder/delete-класс не обходится наследованием)."""
    for text in ("удали всё", "отмени", "сотри последнее"):
        pol = compute_unified_policy(
            text, route_domains(text), prev_open_domains=frozenset({"reminders"}))
        assert "reminders" in pol["allowed_write"], text


def test_r6_window_is_one_turn_338():
    """Окно наследования - ОДИН ход: скан не глубже последнего HumanMessage.
    Ход N+1 без write («спасибо» → текст) закрывает дверь для N+2."""
    hist = _hist_incident() + [
        HumanMessage(content="спасибо"),
        AIMessage(content="Пожалуйста! Обращайся."),
    ]
    assert _prev_open_domains(hist) == set()


def test_r6_withdrawn_result_does_not_open_338():
    """R6 Claude MINOR: #316-отзыв (withdrawn - инструмент НЕ исполнялся) область
    не открывает (защита от дрейфа порядка инжекта withdrawal-сообщений)."""
    hist = [
        HumanMessage(content="поставь напоминание про воду"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="отозвано", name="schedule_reminder", tool_call_id="t1",
                    artifact={"result_kind": "withdrawn"}),
        AIMessage(content="Хорошо, переключаюсь. Что нужно?"),
    ]
    assert _prev_open_domains(hist) == set()


def test_r6_guard_last_message_shapes_338():
    """Защита от дрейфа call-site: последний message = HumanMessage или AIMessage
    с tool_calls → пустой набор (механизм считает только ФИНАЛ хода агента)."""
    hist = _hist_incident()
    assert _prev_open_domains(hist + [HumanMessage(content="В 15")]) == set()
    hist2 = _hist_incident()
    hist2[-1] = AIMessage(content="", tool_calls=[{"name": "list_reminders", "args": {}, "id": "x"}])
    assert _prev_open_domains(hist2) == set()


def test_r7_late_ok_closes_earlier_slot_338():
    """R7 high: слот → уточнение → ok В ТОМ ЖЕ ходе = слот закрыт (последний
    исход домена решает)."""
    hist = [
        HumanMessage(content="поставь напоминание про воду в 12:30"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="уточни время", name="schedule_reminder", tool_call_id="t1",
                    artifact={"result_kind": "time_not_specified"}),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t2"}]),
        ToolMessage(content="ok:scheduled:rem_1:x", name="schedule_reminder", tool_call_id="t2",
                    artifact={"result_kind": "ok"}),
        AIMessage(content="Готово, поставила!"),
    ]
    assert _prev_open_domains(hist) == set()


def test_r7_sticky_and_slot_do_not_stack_338():
    """R7 оба Codex: активная sticky-серия (memory) + открытый слот → continuation
    НЕ применяется (два прямых домена на themeless-ответе = fail-closed)."""
    pol = compute_unified_policy(
        "в 16", route_domains("в 16"),
        sticky_memory_write=True, prev_open_domains=frozenset({"reminders"}))
    assert pol["allowed_write"] == ["memory"]  # только sticky
    assert pol["signals"]["turn_continuation"] == []
