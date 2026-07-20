"""#298 - RED-контракт: дата+время эфемерным хвостом, системный промпт полностью стабилен.

Пинит чеклист приёмки issue #298:
- ON: модель видит строку текущего времени (до минуты) в хвосте контекста;
  вставка ЭФЕМЕРНА - в персист-канон state["messages"] не попадает;
- ON: системный промпт байт-в-байт стабилен (даты в нём нет);
- OFF: байт-в-байт легаси (дата в системном промпте, хвоста нет).
"""

from __future__ import annotations

import re

import pytest

from langchain_core.messages import AIMessage, HumanMessage

from sreda.runtime import react_loop
from tests.unit.conftest import seed_telegram_user

_TIME_RE = re.compile(r"Сейчас \d{4}-\d{2}-\d{2} \(\w+\) \d{2}:\d{2}")


def _flag_time(monkeypatch, on: bool) -> None:
    import sreda.config.settings as sm

    monkeypatch.setenv("SREDA_REACT_TIME_IN_TAIL", "1" if on else "0")
    sm.get_settings.cache_clear()


class _RecordingStubLLM:
    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted = scripted
        self._i = 0
        self.seen_messages: list[list] = []

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        self.seen_messages.append(list(messages))
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


@pytest.mark.asyncio
async def test_on_model_sees_time_ephemeral(db_session, monkeypatch):
    """ON: в контексте модели есть «Сейчас YYYY-MM-DD (день) HH:MM»; персист чист."""
    _flag_time(monkeypatch, True)
    u = seed_telegram_user(db_session)

    stub = _RecordingStubLLM([AIMessage(content="Сейчас примерно десять утра.")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="298-on-1", llm=stub, user_text="а время сколько",
        inbound_message_id="298-on-1-msg", channel="react",
    )
    assert stub.seen_messages, "invoke не вызывался"
    # (i) модель видит строку времени в ХВОСТЕ (последнее human-сообщение)
    last_batch = stub.seen_messages[-1]
    tail_humans = [m for m in last_batch if isinstance(m, HumanMessage)]
    assert tail_humans, "нет human-сообщений в контексте"
    assert _TIME_RE.search(str(tail_humans[-1].content)), (
        f"строка времени не найдена в хвосте: {tail_humans[-1].content!r}")
    # (ii) системный промпт БЕЗ даты (полностью стабилен)
    sys_msg = last_batch[0]
    assert "Сегодня 2" not in str(sys_msg.content), "дата не должна жить в системном промпте при ON"
    # (iii) эфемерность: канон-сообщение пользователя в БАТЧЕ несёт время,
    # но исходный user_text в нём сохранён; повторный ход не видит СТАРОЙ вставки
    stub2 = _RecordingStubLLM([AIMessage(content="Ок.")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="298-on-1", llm=stub2, user_text="спасибо",
        inbound_message_id="298-on-1-msg2", channel="react",
    )
    batch2 = stub2.seen_messages[-1]
    time_hits = [m for m in batch2 if isinstance(m, HumanMessage)
                 and _TIME_RE.search(str(m.content))]
    assert len(time_hits) == 1, (
        f"вставка времени должна быть ЭФЕМЕРНОЙ: в истории второго хода не должно быть "
        f"старых вставок, найдено {len(time_hits)}")


@pytest.mark.asyncio
async def test_off_legacy_byte_identical(db_session, monkeypatch):
    """OFF: дата в системном промпте (как раньше), хвоста времени нет."""
    _flag_time(monkeypatch, False)
    u = seed_telegram_user(db_session)

    stub = _RecordingStubLLM([AIMessage(content="Привет.")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="298-off-1", llm=stub, user_text="привет",
        inbound_message_id="298-off-1-msg", channel="react",
    )
    batch = stub.seen_messages[-1]
    sys_msg = batch[0]
    assert "Сегодня 2" in str(sys_msg.content), "легаси: дата живёт в системном промпте"
    for m in batch:
        if isinstance(m, HumanMessage):
            assert not _TIME_RE.search(str(m.content)), (
                f"при OFF хвостовой вставки времени быть не должно: {m.content!r}")


def test_system_prompt_stable_without_date():
    """ON-режим промпта: без today_str блок даты заменён стабильной строкой."""
    sp_no_date = react_loop._system_prompt("")
    assert "Сегодня 2" not in sp_no_date and "Сегодня  " not in sp_no_date
    assert "в конце последнего сообщения пользователя" in sp_no_date
    # с датой (легаси) - дата на месте
    sp_legacy = react_loop._system_prompt("2026-07-03 (Friday)")
    assert "Сегодня 2026-07-03 (Friday)" in sp_legacy


def test_chat_fact_prompt_stable_without_date():
    from sreda.runtime.react_preflight import chat_fact_system_prompt

    sp_no_date = chat_fact_system_prompt("")
    assert "Сегодня 2" not in sp_no_date
    assert "в конце последнего сообщения пользователя" in sp_no_date
    sp_legacy = chat_fact_system_prompt("2026-07-03 (Friday)")
    assert "2026-07-03" in sp_legacy


# ---------------------------------------------------------------------------
# Фиксы ревью R1 #298
# ---------------------------------------------------------------------------


def test_off_golden_prompt_joints():
    """R1 medium/high: golden-пин легаси-стыков ОБОИХ промптов (пробел-в-пробел) -
    конкатенация после правок не сместила ни байта вокруг даты."""
    from sreda.runtime.react_preflight import chat_fact_system_prompt

    sp = react_loop._system_prompt("2026-07-03 (Friday)")
    assert "<context>\nСегодня 2026-07-03 (Friday). Относительные даты" in sp
    cf = chat_fact_system_prompt("2026-07-03 (Friday)")
    assert "канцелярита.\nСегодня 2026-07-03 (Friday).\nПравила:\n" in cf


@pytest.mark.asyncio
async def test_persisted_user_text_clean(db_session, monkeypatch):
    """R1 medium: канон-история (персист-слой) хранит ЧИСТЫЙ user_text - вставка
    времени не протекает в чекпойнт (пин точным равенством)."""
    _flag_time(monkeypatch, True)
    u = seed_telegram_user(db_session)

    stub = _RecordingStubLLM([AIMessage(content="Около десяти.")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="298-clean-1", llm=stub, user_text="а время сколько",
        inbound_message_id="298-clean-1-msg", channel="react",
    )
    stub2 = _RecordingStubLLM([AIMessage(content="Ок.")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="298-clean-1", llm=stub2, user_text="спасибо",
        inbound_message_id="298-clean-1-msg2", channel="react",
    )
    batch2 = stub2.seen_messages[-1]
    originals = [m for m in batch2 if isinstance(m, HumanMessage)
                 and "а время сколько" in str(m.content)]
    assert originals, "исходное сообщение первого хода не найдено в истории"
    assert str(originals[0].content) == "а время сколько", (
        f"персист-канон должен хранить чистый текст: {originals[0].content!r}")


@pytest.mark.asyncio
async def test_time_before_directives_247(db_session, monkeypatch):
    """R1 high MAJOR: при #247 tail-directives ON директива остаётся ПОСЛЕДНЕЙ
    инструкцией хвоста (порядок: текст → время → директива)."""
    _flag_time(monkeypatch, True)
    import sreda.config.settings as sm
    monkeypatch.setenv("SREDA_REACT_TAIL_DIRECTIVES", "1")
    # директива #215 живёт только на task-пути (eff=="task") → включаем preflight;
    # «покажи дела» матчится слоем-0 _must_task детерминированно (без LLM-классификатора)
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
    sm.get_settings.cache_clear()
    u = seed_telegram_user(db_session)

    stub = _RecordingStubLLM([AIMessage(content="Вот дела.")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="298-dir-1", llm=stub, user_text="покажи дела",
        inbound_message_id="298-dir-1-msg", channel="react",
    )
    # #356: гейт свежести добавляет сценарию ФОРС-проход (стаб не вызвал чтение);
    # контракт «последняя инструкция несёт время» держится и там: директива идёт
    # отдельным trailing-user С якорем времени (R1 субагент CRITICAL - иначе класс
    # #298 возвращался на форс-проходе). Проверяем ПОСЛЕДНИЙ батч как и раньше.
    batch = stub.seen_messages[-1]
    last_human = [m for m in batch if isinstance(m, HumanMessage)][-1]
    content = str(last_human.content)
    m_time = _TIME_RE.search(content)
    i_dir = content.find("ВАЖНО:")
    assert m_time, f"времени нет в хвосте: {content!r}"
    assert i_dir != -1, f"директивы #215/#247 нет в хвосте: {content!r}"
    assert m_time.start() < i_dir, (
        f"директива должна быть ПОСЛЕДНЕЙ инструкцией (время до неё): {content!r}")


@pytest.mark.asyncio
async def test_time_anchor_on_after_tool_pass(db_session, monkeypatch):
    """R2 Claude MAJOR: якорь времени есть на ВСЕХ проходах хода - на синтез-проходе после
    инструментов строка приклеена к ПОСЛЕДНЕМУ user (в середине контекста), отдельный user
    после tool-result НЕ вклинивается (R1 high), строка ЗАМОРОЖЕНА на ход (байты идентичны)."""
    _flag_time(monkeypatch, True)
    u = seed_telegram_user(db_session)

    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "list_checklists", "args": {}, "id": "call_t1",
        }]),
        AIMessage(content="Списков нет."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="298-tool-1", llm=stub, user_text="покажи список кино",
        inbound_message_id="298-tool-1-msg", channel="react",
    )
    assert len(stub.seen_messages) >= 2, "ожидались два прохода (tool_call + синтез)"
    batch1, batch2 = stub.seen_messages[0], stub.seen_messages[-1]
    # (i) хвост второго прохода - tool-result, НЕ вклиненный user
    assert not isinstance(batch2[-1], HumanMessage), (
        f"после tool-result не должно быть вклиненного user: {batch2[-1]!r}")
    # (ii) якорь времени на синтез-проходе ЕСТЬ - в последнем user (середина контекста)
    hits2 = [m for m in batch2 if isinstance(m, HumanMessage) and _TIME_RE.search(str(m.content))]
    assert len(hits2) == 1, f"якорь времени на синтез-проходе: ожидался ровно 1, есть {len(hits2)}"
    # (iii) заморозка: строка времени идентична проходу 1 (intra-turn кеш)
    hits1 = [m for m in batch1 if isinstance(m, HumanMessage) and _TIME_RE.search(str(m.content))]
    assert hits1 and _TIME_RE.search(str(hits1[-1].content)).group(0) == \
        _TIME_RE.search(str(hits2[0].content)).group(0), "строка должна быть заморожена на ход"


@pytest.mark.asyncio
async def test_chat_fact_invoke_gets_time(db_session, monkeypatch):
    """R1 medium MINOR: chat/fact-путь (deepseek) тоже получает время в хвосте invoke."""
    _flag_time(monkeypatch, True)
    import sreda.config.settings as sm
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
    sm.get_settings.cache_clear()
    u = seed_telegram_user(db_session)

    # deepseek-стаб: get_chat_llm(provider=...) возвращает его
    ds = _RecordingStubLLM([AIMessage(content="Сейчас ночь, десять минут второго.")])
    import sreda.services.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_chat_llm", lambda *a, **k: ds)

    # классификатор: форсим интент chat (иначе LLM-классификатор с fail-open task)
    from sreda.runtime import react_preflight as rp
    async def _fake_classify(*_a, **_k):
        return "chat"
    monkeypatch.setattr(rp, "classify_intent", _fake_classify)

    freddie = _RecordingStubLLM([AIMessage(content="не должен вызываться")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="298-chat-1", llm=freddie, user_text="а время сколько",
        inbound_message_id="298-chat-1-msg", channel="react",
    )
    assert ds.seen_messages, "chat/fact-ветка (deepseek) не вызвалась"
    batch = ds.seen_messages[-1]
    last_human = [m for m in batch if isinstance(m, HumanMessage)][-1]
    assert _TIME_RE.search(str(last_human.content)), (
        f"время не дошло до chat/fact invoke: {last_human.content!r}")
    sys_msg = batch[0]
    assert "Сегодня 2" not in str(sys_msg.content)
