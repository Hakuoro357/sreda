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
    assert "в конце диалога" in sp_no_date  # указатель на хвост
    # с датой (легаси) - дата на месте
    sp_legacy = react_loop._system_prompt("2026-07-03 (Friday)")
    assert "Сегодня 2026-07-03 (Friday)" in sp_legacy


def test_chat_fact_prompt_stable_without_date():
    from sreda.runtime.react_preflight import chat_fact_system_prompt

    sp_no_date = chat_fact_system_prompt("")
    assert "Сегодня 2" not in sp_no_date
    assert "в конце диалога" in sp_no_date
    sp_legacy = chat_fact_system_prompt("2026-07-03 (Friday)")
    assert "2026-07-03" in sp_legacy
