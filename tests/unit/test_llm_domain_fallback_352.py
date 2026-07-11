"""#352: LLM-фолбэк доменов на ЕДИНОМ пути - результат классификатора не выбрасывается.

Прод 2026-07-11 16:13 (владелец, канарейка): «Что у меня в списке кино» → список
показан → «я посмотрел машину войны» → «нет инструмента» - ЛОЖНЫЙ отказ (#279).
Корень: фраза без доменных слов → код-роутер молчит → ленивая семья checklists
выгружена. При этом LLM-классификатор classify_domains СУЩЕСТВОВАЛ и ЗВАЛСЯ на
легаси-блоке #221, но единый путь #285 его результат ПЕРЕЗАПИСЫВАЛ (мёртвый вызов:
латентность + деньги в никуда). Владелец: «нахера ты выкидываешь результат ллм?».

Фикс (согласован владельцем 2026-07-11, живые пробы 5/5 + 20/20):
1. classify_domains получает ТИП ПРЕДЫДУЩЕГО ХОДА (разделы, которыми реально
   работал прошлый ход - факт журнала) - проба: 5/5 попаданий вместо 7/10.
2. Результат classified (high) на едином пути → allowed_read (разделы+чтение),
   НИКОГДА не write - запись только кандидатом под человеческий confirm.
3. Классификатор зовётся ТОЛЬКО когда код-слои молчат (route/read_cues/sticky/
   слот-наследование дали пусто) И есть прошлый контекст (prev-turn разделы).
4. Мёртвый двойной вызов в легаси-блоке #221 на unified-тенанте снят.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop
from sreda.runtime.react_loop import _prev_turn_families
from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import (
    DomainClassResult,
    classify_domains,
    route_domains,
)

# ─────────────────── policy: classified → ТОЛЬКО чтение ───────────────────


def test_classified_high_grants_read_only_352():
    """Сценарий владельца: classified high (checklists) → раздел в allowed_read,
    write НЕ открывается (мутация пойдёт кандидатом под confirm)."""
    text = "я посмотрел машину войны"
    pol = compute_unified_policy(
        text, route_domains(text),
        DomainClassResult(("checklists",), "high"))
    assert "checklists" in pol["allowed_read"]
    assert "checklists" not in pol["allowed_write"]


def test_classified_never_writes_even_with_command_352():
    """LLM-домен НИКОГДА не даёт write - даже при императивной команде без
    детерминированного домена («поставь чайник»: w_sig=True, route пуст;
    контракт B1↔B2 «нет домена → кандидат» LLM не обходит)."""
    text = "поставь чайник"
    pol = compute_unified_policy(
        text, route_domains(text),
        DomainClassResult(("shopping",), "high"))
    assert "shopping" in pol["allowed_read"]
    assert pol["allowed_write"] == []


def test_classified_low_ignored_352():
    """low (мусор/несколько/сбой) → не применяется: политика байт-в-байт как без
    классификатора (fail-safe: не угаданный домен)."""
    text = "я посмотрел машину войны"
    route = route_domains(text)
    base = compute_unified_policy(text, route, None)
    low = compute_unified_policy(
        text, route, DomainClassResult(("checklists", "memory"), "low"))
    assert low["allowed_read"] == base["allowed_read"]
    assert low["allowed_write"] == base["allowed_write"]


def test_classified_none_baseline_unchanged_352():
    """Пин: classified=None → прежнее поведение (детерминированный путь не тронут:
    «добавь молоко в покупки» держит прямой write по route)."""
    text = "добавь молоко в покупки"
    pol = compute_unified_policy(text, route_domains(text), None)
    assert "shopping" in pol["allowed_write"]


def test_classified_observability_in_signals_352():
    """Наблюдаемость канарейки: что дал классификатор - в signals (llm_domains)."""
    text = "я посмотрел машину войны"
    pol = compute_unified_policy(
        text, route_domains(text),
        DomainClassResult(("checklists",), "high"))
    assert pol["signals"]["llm_domains"] == ["checklists"]


# ─────────────────── classify_domains: тип предыдущего хода ───────────────────


class _CaptureLLM:
    def __init__(self, reply="checklists"):
        self.calls: list = []
        self._reply = reply

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        return AIMessage(content=self._reply)


def _payload(llm):
    return llm.calls[-1][-1].content


def test_prev_turn_line_in_payload_352():
    """Согласованный формат (проба 5/5): строка «Предыдущий ход работал с
    разделом: checklists» между репликами и текущим сообщением."""
    llm = _CaptureLLM()
    asyncio.run(classify_domains(
        [HumanMessage(content="Что у меня в списке кино")],
        "я посмотрел машину войны", llm,
        prev_turn_domains=("checklists",)))
    p = _payload(llm)
    assert "Предыдущий ход работал с разделом: checklists" in p
    assert p.index("Последние реплики:") < p.index("Предыдущий ход работал") \
        < p.index("Текущее сообщение пользователя:")


def test_prev_turn_plural_form_352():
    llm = _CaptureLLM()
    asyncio.run(classify_domains(
        [], "и что дальше", llm, prev_turn_domains=("checklists", "shopping")))
    assert "Предыдущий ход работал с разделами: checklists, shopping" in _payload(llm)


def test_no_prev_turn_line_without_arg_352():
    """Без параметра - запрос байт-в-байт прежний (легаси-вызовы не меняются)."""
    llm = _CaptureLLM()
    asyncio.run(classify_domains(
        [HumanMessage(content="привет")], "какая погода", llm))
    assert "Предыдущий ход" not in _payload(llm)


# ─────────────────── _prev_turn_families: факт журнала ───────────────────


def _tm(name, cid="t1", content="ok"):
    return ToolMessage(content=content, name=name, tool_call_id=cid)


def _closed_turn(*tool_names, user="Что у меня в списке кино"):
    msgs: list = [HumanMessage(content=user)]
    for i, n in enumerate(tool_names):
        msgs.append(AIMessage(content="", tool_calls=[{"name": n, "args": {}, "id": f"t{i}"}]))
        msgs.append(_tm(n, f"t{i}"))
    msgs.append(AIMessage(content="Вот."))
    return msgs


def test_prev_families_from_last_turn_352():
    assert _prev_turn_families(_closed_turn("get_checklist")) == ("checklists",)


def test_prev_families_only_last_turn_352():
    """Считается ТОЛЬКО последний ход: работа с покупками ходом раньше не тянется."""
    msgs = _closed_turn("add_shopping_items", user="добавь молоко в покупки") \
        + _closed_turn("get_checklist")
    assert _prev_turn_families(msgs) == ("checklists",)


def test_prev_families_meta_and_unknown_skipped_352():
    """ask_human/галлюцинированные имена - не разделы."""
    msgs = _closed_turn("ask_human", "totally_fake_tool")
    assert _prev_turn_families(msgs) == ()


def test_prev_families_non_user_domains_filtered_352():
    """Семьи вне пользовательского enum классификатора (onboarding/ui/utility)
    не передаются - хинт только из слов, которые классификатор понимает."""
    msgs = _closed_turn("onboarding_answered", "reply_with_buttons")
    assert _prev_turn_families(msgs) == ()


def test_prev_families_dedup_sorted_352():
    msgs = _closed_turn("get_checklist", "list_checklist_items", "list_shopping")
    assert _prev_turn_families(msgs) == ("checklists", "shopping")


def test_prev_families_web_excluded_352():
    """R1 субагент MAJOR: web - baseline, не own-data контекст (зеркало
    _prev_open_domains). Смешанный ход «погода + список» не должен подсовывать
    классификатору web-дистрактор (иначе кейс #352 ломается заново)."""
    assert _prev_turn_families(_closed_turn("web_search", "get_weather")) == ()
    assert _prev_turn_families(
        _closed_turn("web_search", "get_checklist")) == ("checklists",)


def test_parse_domains_none_dominates_352():
    """R2 sol MAJOR: болтливый ответ «none — это не checklists» НЕ должен
    превращаться в checklists/high - любое упоминание none = пусто/low."""
    from sreda.runtime.react_preflight import _parse_domains
    assert _parse_domains("none — это не checklists").domains == ()
    assert _parse_domains("none").confidence == "low"
    # обычные ответы не задеты
    assert _parse_domains("checklists").domains == ("checklists",)
    assert _parse_domains("checklists").confidence == "high"


def test_prev_families_not_executed_kinds_excluded_352():
    """R2 sol MAJOR: галлюцинированный/заблокированный вызов (структурный
    result_kind неисполнения) - НЕ «работал с разделом»; иначе галлюцинация
    планировщика открывала бы закрытый раздел через LLM-фолбэк."""
    for kind in ("domain_blocked", "unavailable", "family_not_loaded", "withdrawn"):
        msgs = [
            HumanMessage(content="привет"),
            AIMessage(content="", tool_calls=[
                {"name": "list_shopping", "args": {}, "id": "t1"}]),
            ToolMessage(content="закрыто", name="list_shopping", tool_call_id="t1",
                        artifact={"result_kind": kind}),
            AIMessage(content="Не могу."),
        ]
        assert _prev_turn_families(msgs) == (), kind


def test_prev_families_declined_confirms_count_352():
    """R2-субагент (пере-решение R1 terra): отклонённый confirm - кандидатный
    («Хорошо, не делаю.») и деструктивный («Хорошо, не трогаю.», result_kind=ok
    прод-формы) - СЧИТАЕТСЯ темой хода: «нет, не удаляй… а что там?» продолжает
    раздел. Открывается только чтение; мутации всегда под confirm."""
    from sreda.runtime.react_loop import _CONFIRM_DECLINED_TEXT
    candidate_declined = [
        HumanMessage(content="отметь машину войны"),
        AIMessage(content="", tool_calls=[
            {"name": "mark_checklist_item_done", "args": {}, "id": "t1"}]),
        ToolMessage(content=_CONFIRM_DECLINED_TEXT, name="mark_checklist_item_done",
                    tool_call_id="t1"),
        AIMessage(content="Хорошо, не отмечаю."),
    ]
    assert _prev_turn_families(candidate_declined) == ("checklists",)
    destructive_declined = [
        HumanMessage(content="удали список кино"),
        AIMessage(content="", tool_calls=[
            {"name": "archive_checklist", "args": {}, "id": "t1"}]),
        ToolMessage(content="Хорошо, не трогаю.", name="archive_checklist",
                    tool_call_id="t1", artifact={"result_kind": "ok"}),
        AIMessage(content="Хорошо, не трогаю."),
    ]
    assert _prev_turn_families(destructive_declined) == ("checklists",)


def test_prev_families_empty_cases_352():
    assert _prev_turn_families(None) == ()
    assert _prev_turn_families([]) == ()
    # незакрытый ход (последнее сообщение не финальный AIMessage) - не считаем
    assert _prev_turn_families([HumanMessage(content="привет")]) == ()
    # ход вовсе без инструментов
    assert _prev_turn_families(
        [HumanMessage(content="привет"), AIMessage(content="Привет!")]) == ()


# ─────────────────── e2e через handle_turn (паттерн #350) ───────────────────


class _NoTrace:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _Chat:
    """Планировщик = очередь invoke; классификаторы идут в ainvoke: домен-
    классификатор детектится по системке и отвечает reply (счёт+payload),
    прочие async-вызовы (classify_intent) - как в остальных e2e: исключение →
    fail-open (у моков других тестов ainvoke нет вовсе)."""

    def __init__(self, name, responses=None, domain_reply="checklists"):
        self.name = name
        self._responses = list(responses or [])
        self.domain_calls: list = []
        self._domain_reply = domain_reply

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return AIMessage(content="ок")

    async def ainvoke(self, messages, **kwargs):
        sys = str(getattr(messages[0], "content", ""))
        if "классификатор РАЗДЕЛА" in sys:
            self.domain_calls.append(str(getattr(messages[-1], "content", "")))
            return AIMessage(content=self._domain_reply)
        raise RuntimeError("ainvoke not mocked for this classifier")


def _clean_tool(name, inv):
    def _f(**kwargs):
        inv[name] = inv.get(name, 0) + 1
        return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=name)


_CORE = ["get_checklist", "list_checklist_items", "mark_checklist_item_done",
         "list_reminders", "schedule_reminder", "add_task",
         "need_family", "recall_memory", "ask_human"]
_WEB = ["web_search", "fetch_url", "get_weather"]


@pytest.fixture
def install(monkeypatch):
    from sreda.config import settings as settings_mod

    def _install(*, responses, domain_reply="checklists", confirm_answer="да"):
        # прод-конфиг: preflight + единый путь + доменный роутер execute (#221) +
        # обрезка набора (#165) - конфигурация инцидента
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_TENANTS", "t")
        monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
        monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "*")
        monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "*")
        settings_mod.get_settings.cache_clear()
        inv: dict = {}
        monkeypatch.setattr(react_loop, "build_slice_tools", lambda *a, **k: [
            _clean_tool(n, inv) for n in (_CORE + _WEB)])
        monkeypatch.setattr(react_loop, "_trace", _NoTrace())
        monkeypatch.setattr(react_loop, "_record_react_usage", lambda **k: None)
        # кандидат-confirm: счётчик доказывает, что мутация шла через ярус (б)
        # (R1 sol/terra/субагент: «мутация произошла» ≠ «мутация была под confirm»)
        def _interrupt(payload):
            inv["interrupt"] = inv.get("interrupt", 0) + 1
            return confirm_answer
        monkeypatch.setattr(react_loop, "interrupt", _interrupt)
        import sreda.services.llm as llm_mod
        freddie = _Chat("freddie", responses=responses, domain_reply=domain_reply)
        monkeypatch.setattr(llm_mod, "get_chat_llm", lambda *a, **k: _Chat("ds"))
        return inv, freddie

    yield _install
    settings_mod.get_settings.cache_clear()


def _turn(freddie, thread, text):
    return asyncio.run(react_loop.handle_turn(
        session=None, tenant_id="t", user_id="u", thread_id=thread,
        llm=freddie, user_text=text, inbound_message_id=f"{thread}:{text[:10]}",
        channel="react", resume_only=False, expected_confirm_id="",
        provider_key="inception-mercury2", fallback_llm=None))


def _ai_call(tool, cid, **args):
    return AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": cid}])


def test_e2e_owner_scenario_352(install):
    """Сценарий владельца: список показан → «я посмотрел машину войны» →
    классификатор (с типом предыдущего хода) → семья загружена → отметка
    исполнена (через кандидат-confirm), ложного «нет инструмента» нет."""
    inv, freddie = install(responses=[
        _ai_call("list_checklist_items", "c1", name="кино"),
        AIMessage(content="Вот твой список кино: Скорпион, Машина войны."),
        _ai_call("mark_checklist_item_done", "c2", name="кино", item="Машина войны"),
        AIMessage(content="Отметила «Машина войны» выполненной."),
    ])
    _turn(freddie, "s1", "Что у меня в списке кино")
    assert inv.get("list_checklist_items", 0) == 1
    assert freddie.domain_calls == [], "route дал домен - классификатор не зовётся"
    _turn(freddie, "s1", "я посмотрел машину войны")
    assert len(freddie.domain_calls) == 1, \
        "ровно ОДИН вызов классификатора (мёртвый легаси-дубль снят)"
    assert "Предыдущий ход работал с разделом: checklists" in freddie.domain_calls[0]
    assert "я посмотрел машину войны" in freddie.domain_calls[0]
    assert inv.get("interrupt", 0) == 1, \
        "мутация шла через кандидат-confirm (ярус б), не прямым write"
    assert inv.get("mark_checklist_item_done", 0) == 1, \
        "отметка исполнена (кандидат подтверждён) - ложного отказа нет"


def test_e2e_owner_scenario_declined_no_mutation_352(install):
    """R1: «нет» на кандидат-confirm → мутации НЕТ (LLM-открытый раздел не
    может писать мимо человека)."""
    inv, freddie = install(responses=[
        _ai_call("list_checklist_items", "c1", name="кино"),
        AIMessage(content="Вот список."),
        _ai_call("mark_checklist_item_done", "c2", name="кино", item="Машина войны"),
        AIMessage(content="Хорошо, не трогаю."),
    ], confirm_answer="нет")
    _turn(freddie, "s1n", "Что у меня в списке кино")
    _turn(freddie, "s1n", "я посмотрел машину войны")
    assert inv.get("interrupt", 0) == 1
    assert inv.get("mark_checklist_item_done", 0) == 0, "отказ → мутации нет"


def test_e2e_smalltalk_without_context_no_classifier_352(install):
    """Анти-регресс (мина #285): безтемный ход БЕЗ прошлого контекста -
    классификатор НЕ зовётся вовсе (в т.ч. мёртвый легаси-вызов снят),
    own-data не открывается."""
    inv, freddie = install(responses=[AIMessage(content="Всё отлично!")])
    r = _turn(freddie, "s2", "как дела?")
    assert freddie.domain_calls == [], "нет прошлого контекста - LLM не дёргаем"
    assert inv == {}, "никакие own-data инструменты не исполнялись"
    assert "отлично" in str(r).lower()


def test_e2e_slot_answer_no_classifier_352(install):
    """Слот-ответ («3 раза» на вопрос о конце) - политика непустая через
    слот-наследование → классификатор не зовётся (латентность не добавляем)."""
    inv, freddie = install(responses=[
        _ai_call("schedule_reminder", "c1", title="вода",
                 trigger_iso="2026-07-12T13:30:00+03:00",
                 recurrence_rule="FREQ=HOURLY"),
        AIMessage(content="До какого времени повторять или сколько раз?"),
        _ai_call("schedule_reminder", "c2", title="вода",
                 trigger_iso="2026-07-12T13:30:00+03:00",
                 recurrence_rule="FREQ=HOURLY;COUNT=3"),
        AIMessage(content="Готово."),
    ])
    _turn(freddie, "s3", "поставь завтра напоминание с 13:30 каждый час пить воду")
    _turn(freddie, "s3", "3 раза")
    assert freddie.domain_calls == [], "код-слои не молчат - LLM не нужен"


def test_e2e_classifier_outside_prev_turn_not_applied_352(install):
    """R1 sol/terra (инъекция-канал): high-домен ВНЕ разделов прошлого хода
    (memory при prev-turn=checklists) НЕ применяется - LLM не авторизует НОВЫЕ
    разделы, только переоткрывает раздел, которым юзер сам работал."""
    inv, freddie = install(responses=[
        _ai_call("list_checklist_items", "c1", name="кино"),
        AIMessage(content="Вот список."),
        _ai_call("recall_memory", "c2", query="машина войны"),
        AIMessage(content="Не разобралась, уточни."),
    ], domain_reply="memory")
    _turn(freddie, "s4", "Что у меня в списке кино")
    _turn(freddie, "s4", "я посмотрел машину войны")
    assert len(freddie.domain_calls) == 1
    assert inv.get("recall_memory", 0) == 0, "memory ⊄ prev-turn → раздел не открыт"
    assert inv.get("mark_checklist_item_done", 0) == 0


def test_e2e_route_domain_without_cue_no_classifier_352(install):
    """R1 sol MAJOR-1: route увидел домен, но кюсов нет («рецепты сложные») -
    это ОСОЗНАННОЕ неоткрытие own-data (мина #285), а не молчание кода;
    классификатор не зовётся."""
    inv, freddie = install(responses=[
        _ai_call("list_checklist_items", "c1", name="кино"),
        AIMessage(content="Вот список."),
        AIMessage(content="Да, бывает."),
    ])
    _turn(freddie, "s5", "Что у меня в списке кино")
    _turn(freddie, "s5", "рецепты сложные")
    assert freddie.domain_calls == [], "route-домен без кюса → LLM не дёргаем"


def test_e2e_smalltalk_after_tools_none_applied_nothing_352(install):
    """R1 terra MAJOR-4: смолток после инструментального хода («спасибо») -
    классификатор отвечает none → строгий парс даёт low → скоуп не открывается."""
    inv, freddie = install(responses=[
        _ai_call("list_checklist_items", "c1", name="кино"),
        AIMessage(content="Вот список."),
        AIMessage(content="Пожалуйста!"),
    ], domain_reply="none")
    _turn(freddie, "s6", "Что у меня в списке кино")
    r = _turn(freddie, "s6", "спасибо")
    assert len(freddie.domain_calls) == 1, "вызов был (продолжение не отличить кодом)"
    assert inv.get("mark_checklist_item_done", 0) == 0
    assert "пожалуйста" in str(r).lower()


def test_e2e_unified_failure_full_failopen_352(install, monkeypatch):
    """R1 sol/terra MAJOR (аварийный режим): сбой unified-политики → ПОЛНЫЙ
    fail-open (набор без deny-фильтра, как при сбое легаси-роутинга), ход жив -
    НЕ deny с ложными отказами класса #352."""
    from sreda.runtime import react_policy as _rp

    def _boom(*a, **k):
        raise RuntimeError("352-fault-injection")
    monkeypatch.setattr(_rp, "compute_unified_policy", _boom)
    inv, freddie = install(responses=[
        _ai_call("list_reminders", "c1"),
        AIMessage(content="Вот напоминания."),
    ])
    r = _turn(freddie, "s7", "я посмотрел машину войны")
    assert inv.get("list_reminders", 0) == 1, \
        "fail-open: инструменты доступны (deny-фильтра нет), ход не упал"
    assert "напоминани" in str(r).lower()


def test_e2e_failure_midstream_then_recovers_352(install, monkeypatch):
    """R2 sol/terra MAJOR (залипание unified_execute): успешный unified-ход →
    сбой policy на следующем ходе (сброс каналов, fail-open) → третий ход снова
    ШТАТНЫЙ unified (классификатор + кандидат-confirm работают)."""
    from sreda.runtime import react_policy as _rp
    _orig = _rp.compute_unified_policy
    inv, freddie = install(responses=[
        _ai_call("list_checklist_items", "c1", name="кино"),
        AIMessage(content="Вот список."),
        _ai_call("list_reminders", "c2"),
        AIMessage(content="Вот напоминания."),
        _ai_call("list_checklist_items", "c3", name="кино"),
        AIMessage(content="Вот список ещё раз."),
        _ai_call("mark_checklist_item_done", "c4", name="кино", item="Машина войны"),
        AIMessage(content="Отметила."),
    ])
    _turn(freddie, "s8", "Что у меня в списке кино")           # ход 1: unified, успех
    monkeypatch.setattr(_rp, "compute_unified_policy",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _turn(freddie, "s8", "перескажи напоминания")               # ход 2: сбой → fail-open
    assert inv.get("list_reminders", 0) == 1, "fail-open: ход 2 жив"
    monkeypatch.setattr(_rp, "compute_unified_policy", _orig)
    _turn(freddie, "s8", "Что у меня в списке кино")            # ход 3: штатный unified
    assert inv.get("list_checklist_items", 0) == 2, "ход 3: unified снова работает"
    _turn(freddie, "s8", "я посмотрел машину войны")            # ход 4: классификатор
    assert len(freddie.domain_calls) == 1, "ход 4: классификатор снова работает"
    assert inv.get("interrupt", 0) == 1, "ход 4: кандидат-confirm (ярус б) работает"
    assert inv.get("mark_checklist_item_done", 0) == 1
