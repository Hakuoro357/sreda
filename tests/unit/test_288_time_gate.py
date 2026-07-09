"""#288 — механический гейт времени (возврат #180 «не хватает инфо — спроси, особенно ВРЕМЯ»).

Прод-кейс 01.07 (tenant_tg_5006894546): «напомни об этом 8 числа» (дата БЕЗ времени) → бот молча создал
напоминание на выдуманные 13:00. Промпт (докстринг schedule_reminder) слабую модель не держал. Гейт на
диспатче: конкретный момент в аргументе + НЕТ упоминания времени в окне хода (текст юзера + ответы
ask_human) → структурный отказ time_not_specified, не создание. Красная линия «врёт об успехе».
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop
from sreda.runtime.react_signals import text_mentions_time


# ─────────── детектор: что считается ВРЕМЕНЕМ ───────────
def test_time_detector_positive():
    for t in ("в 9", "в 13:00", "9.30 подойдёт", "в девять утра", "в час дня", "через час",
              "через 30 минут", "через полчаса", "в полдень", "в полночь", "в 21 час",
              "утром", "вечером напомни", "днём", "с утра", "после обеда",
              "завтра в 10", "14 июля в 9 утра"):
        assert text_mentions_time(t), t


def test_time_detector_negative_dates_only():
    # голая дата/день — НЕ время (ядро кейса Ольги)
    for t in ("напомни об этом 14 числа", "завтра", "напомни 8 числа про стоматолога",
              "9 июля", "в пятницу", "на следующей неделе", "напомни послезавтра",
              "Ане 15 июля нужно к стоматологу. Напомни мне об этом 14 числа"):
        assert not text_mentions_time(t), t


def test_time_detector_v9_july_is_date():
    # «в 9 июля»-подобные (цифра + месяц) — дата, не «в 9 (часов)»
    for t in ("напомни в 9 июля", "встреча 9 июля", "в 14 числа"):
        assert not text_mentions_time(t), t


# ─────────── окно хода: текст юзера + ответы ask_human ───────────
def test_turn_time_window_includes_ask_human_answers():
    msgs = [
        HumanMessage(content="напомни про стоматолога 14 числа"),
        AIMessage(content="", tool_calls=[{"name": "ask_human", "args": {}, "id": "q1"}]),
        ToolMessage(content="в 13", tool_call_id="q1", name="ask_human"),
    ]
    w = react_loop._turn_time_window_text(msgs)
    assert "14 числа" in w and "в 13" in w
    assert text_mentions_time(w)  # двухходовка: ответ «в 13» делает окно валидным
    # без ответа — времени нет
    assert not text_mentions_time(react_loop._turn_time_window_text(msgs[:1]))


# ─────────── e2e через handle_turn (чеклист issue) ───────────
class _NoTrace:
    def __getattr__(self, _):
        return lambda *a, **k: None


class _Chat:
    def __init__(self, label, *, responses=None):
        self.label = label
        self._responses, self._i = list(responses or []), 0

    async def ainvoke(self, _msgs):
        return AIMessage(content="chat")

    def bind_tools(self, tools):
        outer = self

        def _inv(_msgs):
            if outer._responses:
                r = outer._responses[min(outer._i, len(outer._responses) - 1)]
                outer._i += 1
                return r
            return AIMessage(content="resp")
        return RunnableLambda(_inv)


def _clean_tool(name, inv):
    if name == "ask_human":  # как настоящий: interrupt-пауза, ответ юзера → return (→ ToolMessage)
        from langgraph.types import interrupt

        def _ask(question: str = "") -> str:
            inv["ask_human"] = inv.get("ask_human", 0) + 1
            return str(interrupt(question))
        return StructuredTool.from_function(func=_ask, name="ask_human", description="ask")

    def _f(title: str = "", trigger_iso: str = "", reminder_ref: str = "", q: str = "") -> str:
        inv[name] = inv.get(name, 0) + 1
        return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=name)


_CORE = ["list_reminders", "schedule_reminder", "update_reminder", "add_task", "cancel_task",
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


def test_e2e_date_without_time_gated(install):
    """Кейс Ольги (red из чеклиста): дата без времени + модель выдумала 13:00 → напоминание НЕ создано,
    отказ time_not_specified дошёл до модели, финал — вопрос."""
    inv, freddie = install(responses=[
        _ai_call("schedule_reminder", "c1", title="стоматолог", trigger_iso="2026-07-14T13:00:00+03:00"),
        AIMessage(content="Во сколько напомнить?"),
    ])
    r = _turn(freddie, "g1", "Ане 15 июля нужно к стоматологу. Напомни мне об этом 14 числа")
    assert inv.get("schedule_reminder", 0) == 0, "выдуманное время НЕ должно создать напоминание"
    assert "сколько" in str(r).lower(), r


def test_e2e_explicit_time_passes(install):
    """Кейс Екатерины (анти-регресс): явное время → создаётся без лишних вопросов."""
    inv, freddie = install(responses=[
        _ai_call("schedule_reminder", "c1", title="репетиция", trigger_iso="2026-07-16T09:00:00+03:00"),
        AIMessage(content="Готово, записала на 9 утра."),
    ])
    _turn(freddie, "g2", "напомни 16 июля в 9 утра про репетицию")
    assert inv.get("schedule_reminder", 0) == 1, "явное время должно пройти гейт"


def test_e2e_two_hop_answer_passes(install):
    """Двухходовка (чеклист п.3): «во сколько?» через ask_human → ответ «в 13» → создание проходит
    (окно хода видит ответ на уточнение)."""
    inv, freddie = install(responses=[
        _ai_call("ask_human", "q1", question="Во сколько напомнить?"),   # ход1 → пауза
        _ai_call("schedule_reminder", "c1", title="стоматолог",           # resume: время из ответа
                 trigger_iso="2026-07-14T13:00:00+03:00"),
        AIMessage(content="Готово, напомню 14-го в 13:00."),
    ])
    r1 = _turn(freddie, "g3", "напомни про стоматолога 14 числа")
    assert "сколько" in str(r1).lower(), r1      # пауза-вопрос дошла до юзера
    assert inv.get("schedule_reminder", 0) == 0  # до ответа — не создано
    _turn(freddie, "g3", "в 13")                  # ответ на ask_human (resume)
    assert inv.get("schedule_reminder", 0) == 1, "ответ «в 13» должен открыть гейт (окно хода)"


def test_e2e_update_title_only_not_gated(install):
    """update_reminder БЕЗ смены времени (title-only) — не гейтится."""
    inv, freddie = install(responses=[
        _ai_call("update_reminder", "c1", reminder_ref="r1", title="новое название"),
        AIMessage(content="Переименовала."),
    ])
    r1 = _turn(freddie, "g4", "переименуй напоминание в новое название")
    if getattr(r1, "awaiting_confirm", False):  # кандидат (нет сигнала) → подтверждаем
        _turn(freddie, "g4", "да")
    assert inv.get("update_reminder", 0) == 1, "title-only update не должен гейтиться временем"
