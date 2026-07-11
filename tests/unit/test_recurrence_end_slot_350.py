"""#350: конец повторения - СТРУКТУРНЫЙ слот (как время #180), не промпт-инструкция.

Прод 2026-07-11 13:30 (владелец, алерт fallback_used passes:3): «Поставь завтра
напоминание с 13:30 каждый час» → хинт #333 велел модели уточнить конец ДО
вызова инструмента → слот-исхода в журнале НЕТ → цепочка окна (#349) и
наследование (#338) не сцепились → «3 раза» пришло в ход без «13:30» → гейт
времени честно отбил → деградация.

Фикс: диспатч-гейт recurrence_end_not_specified - schedule_reminder с
recurrence_rule БЕЗ COUNT/UNTIL получает структурный слот-исход (модель спросит
конец), ОДИН раз на серию (повторный вызов без конца после уже заданного вопроса
= юзер сказал бессрочно/неявно → ставим бессрочное, не мучаем). Слот-кинд входит
в цепочку окна #349 и наследование #338.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime.react_loop import (
    _prev_open_domains,
    _recurrence_end_already_asked,
    _turn_time_window_text,
)


def _mk_slot_msgs(kind: str):
    return [
        HumanMessage(content="Поставь завтра напоминание с 13:30 каждый час"),
        AIMessage(content="", tool_calls=[{"name": "schedule_reminder", "args": {}, "id": "t1"}]),
        ToolMessage(content="уточни конец", name="schedule_reminder", tool_call_id="t1",
                    artifact={"result_kind": kind}),
        AIMessage(content="До какого времени повторять или сколько раз?"),
        HumanMessage(content="3 раза"),
    ]


def test_end_slot_extends_time_window_350():
    """Сценарий владельца: «3 раза» после слот-вопроса о конце - «13:30» из
    первого сообщения ВИДНО гейту времени."""
    w = _turn_time_window_text(_mk_slot_msgs("recurrence_end_not_specified"))
    assert "13:30" in w


def test_end_slot_opens_domain_350():
    """Слот конца открывает reminders для наследования (#338) - «3 раза» без
    кандидат-confirm."""
    msgs = _mk_slot_msgs("recurrence_end_not_specified")[:-1]
    assert "reminders" in _prev_open_domains(msgs)


def test_end_already_asked_detector_350():
    """Одноразовость: детектор видит, что конец УЖЕ спрашивали в серии."""
    msgs = _mk_slot_msgs("recurrence_end_not_specified")
    assert _recurrence_end_already_asked(msgs)
    assert not _recurrence_end_already_asked(_mk_slot_msgs("time_not_specified"))


# ── e2e через handle_turn (паттерн test_288_time_gate) ──────────────────────

import asyncio

import pytest
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop


class _NoTrace:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _Chat:
    def __init__(self, name, responses=None):
        self.name = name
        self._responses = list(responses or [])

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return AIMessage(content="ок")


def _clean_tool(name, inv):
    if name == "schedule_reminder":
        # явная сигнатура: **kwargs-мок теряет аргументы (пустая схема LangChain)
        def _f(title="", trigger_iso="", recurrence_rule=""):
            inv[name] = inv.get(name, 0) + 1
            inv[f"{name}_args"] = {"title": title, "trigger_iso": trigger_iso,
                                   "recurrence_rule": recurrence_rule}
            return f"{name}-ok"
    else:
        def _f(**kwargs):
            inv[name] = inv.get(name, 0) + 1
            return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=name)


_CORE = ["list_reminders", "schedule_reminder", "update_reminder", "add_task",
         "need_family", "recall_memory", "ask_human"]
_WEB = ["web_search", "fetch_url", "get_weather"]


@pytest.fixture
def install(monkeypatch):
    from sreda.config import settings as settings_mod

    def _install(*, responses):
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_TENANTS", "t")
        settings_mod.get_settings.cache_clear()
        inv: dict = {}
        monkeypatch.setattr(react_loop, "build_slice_tools", lambda *a, **k: [
            _clean_tool(n, inv) for n in (_CORE + _WEB)])
        monkeypatch.setattr(react_loop, "_trace", _NoTrace())
        monkeypatch.setattr(react_loop, "_record_react_usage", lambda **k: None)
        import sreda.services.llm as llm_mod
        freddie = _Chat("freddie", responses=responses)
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


def _ai_call(name, cid, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def test_e2e_owner_scenario_full_series_350(install):
    """Сценарий владельца: «с 13:30 каждый час» → гейт конца спрашивает →
    «3 раза» → создано с COUNT=3 (время 13:30 видно через слот-цепочку)."""
    inv, freddie = install(responses=[
        _ai_call("schedule_reminder", "c1", title="тест",
                 trigger_iso="2026-07-12T13:30:00+03:00",
                 recurrence_rule="FREQ=HOURLY"),
        AIMessage(content="До какого времени повторять или сколько раз?"),
        _ai_call("schedule_reminder", "c2", title="тест",
                 trigger_iso="2026-07-12T13:30:00+03:00",
                 recurrence_rule="FREQ=HOURLY;COUNT=3"),
        AIMessage(content="Готово, поставила: каждый час, всего 3 раза."),
    ])
    r1 = _turn(freddie, "s1", "Поставь завтра напоминание с 13:30 каждый час")
    assert inv.get("schedule_reminder", 0) == 0, "без конца - НЕ создаётся, гейт спрашивает"
    assert "сколько" in str(r1).lower() or "какого" in str(r1).lower(), r1
    _turn(freddie, "s1", "3 раза")
    assert inv.get("schedule_reminder", 0) == 1, "ответ «3 раза» → создание (окно видит 13:30)"
    assert inv["schedule_reminder_args"]["recurrence_rule"] == "FREQ=HOURLY;COUNT=3"


def test_e2e_end_named_upfront_no_question_350(install):
    """Конец назван сразу («до 18») → создаётся без вопроса о конце."""
    inv, freddie = install(responses=[
        _ai_call("schedule_reminder", "c1", title="вода",
                 trigger_iso="2026-07-12T13:30:00+03:00",
                 recurrence_rule="FREQ=HOURLY;UNTIL=20260712T150000Z"),
        AIMessage(content="Готово."),
    ])
    _turn(freddie, "s2", "поставь завтра напоминание с 13:30 каждый час до 18 пить воду")
    assert inv.get("schedule_reminder", 0) == 1


def test_e2e_asked_once_then_endless_allowed_350(install):
    """Одноразовость: вопрос о конце уже был, юзер ответил неявно → повторный
    вызов БЕЗ COUNT/UNTIL проходит (бессрочное, не мучаем повтором)."""
    inv, freddie = install(responses=[
        _ai_call("schedule_reminder", "c1", title="вода",
                 trigger_iso="2026-07-12T13:30:00+03:00",
                 recurrence_rule="FREQ=HOURLY"),
        AIMessage(content="До какого времени повторять или сколько раз?"),
        _ai_call("schedule_reminder", "c2", title="вода",
                 trigger_iso="2026-07-12T13:30:00+03:00",
                 recurrence_rule="FREQ=HOURLY"),
        AIMessage(content="Хорошо, поставила без конца - отменишь когда захочешь."),
    ])
    _turn(freddie, "s3", "поставь завтра напоминание с 13:30 каждый час пить воду")
    assert inv.get("schedule_reminder", 0) == 0
    _turn(freddie, "s3", "пока не отменю")
    assert inv.get("schedule_reminder", 0) == 1, "второй раз конец не требуем"


def test_e2e_oneshot_reminder_not_gated_350(install):
    """Анти-регресс: разовое напоминание (без recurrence_rule) гейт конца
    не трогает."""
    inv, freddie = install(responses=[
        _ai_call("schedule_reminder", "c1", title="врач",
                 trigger_iso="2026-07-12T09:00:00+03:00"),
        AIMessage(content="Готово."),
    ])
    _turn(freddie, "s4", "напомни завтра в 9 про врача")
    assert inv.get("schedule_reminder", 0) == 1
