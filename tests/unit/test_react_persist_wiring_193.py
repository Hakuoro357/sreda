"""#193 Phase B — проводка durable-персистентности в react_loop.

Флаг-точка ключа (_build_thread_config) + фабрика checkpointer (_get_checkpointer) + clear_pending на
РЕАЛЬНОМ compiled-графе с interrupt (durable через новый инстанс saver'а = «рестарт»; resume→re-interrupt;
история цела) + async-обёртки (to_thread).
"""
from __future__ import annotations

from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from sreda.config import settings as st_mod
from sreda.db.base import Base
from sreda.runtime import react_loop as RL
from sreda.runtime.react_checkpoint_saver import EncryptedSqlCheckpointSaver


@pytest.fixture()
def _persist_off(monkeypatch):
    monkeypatch.delenv("SREDA_REACT_PERSIST_ENABLED", raising=False)
    st_mod.get_settings.cache_clear()
    yield
    st_mod.get_settings.cache_clear()


@pytest.fixture()
def _persist_on(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_PERSIST_ENABLED", "1")
    st_mod.get_settings.cache_clear()
    RL._PERSIST_SAVER = None  # сброс синглтона между тестами
    yield
    st_mod.get_settings.cache_clear()
    RL._PERSIST_SAVER = None


def test_flag_off_keeps_gen_key(_persist_off):
    cfg = RL._build_thread_config("react:t1:hmacX", 2)
    assert cfg["configurable"]["thread_id"] == "react:t1:hmacX#2"  # gen в ключе
    assert "checkpoint_ns" not in cfg["configurable"]
    assert isinstance(RL._get_checkpointer(), InMemorySaver)


def test_flag_on_stable_key_and_saver(_persist_on):
    cfg = RL._build_thread_config("react:t1:hmacX", 2)
    # СТАБИЛЬНЫЙ ключ с версией в ПРЕФИКСЕ (checkpoint_ns зарезервирован LangGraph под подграфы), gen НЕ в ключе
    assert cfg["configurable"]["thread_id"] == "react-v1:react:t1:hmacX"
    assert "checkpoint_ns" not in cfg["configurable"]  # ns="" штатно (не версия топологии)
    assert RL._durable_thread_id("react:t1:hmacX") == "react-v1:react:t1:hmacX"
    assert isinstance(RL._get_checkpointer(), EncryptedSqlCheckpointSaver)


# --- реальный compiled-граф с interrupt -----------------------------------

class _S(TypedDict):
    messages: Annotated[list, add_messages]


def _ask_node(state):
    ans = interrupt("подтверди?")
    return {"messages": [AIMessage(content=f"ok:{ans}")]}


def _build_interrupt_graph(saver):
    g = StateGraph(_S)
    g.add_node("ask", _ask_node)
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    return g.compile(checkpointer=saver)


@pytest.fixture()
def _file_sf():
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # NullPool: каждая сессия = своё соединение (закрывается после op) — LangGraph пишет checkpoint
    # в фоновом потоке; shared-соединение (StaticPool) путало бы транзакции между потоками.
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SF
    engine.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


def test_interrupt_survives_restart_and_clear_pending(_file_sf):
    """РЕАЛЬНЫЙ граф: interrupt → durable через новый инстанс saver (рестарт) → clear_pending снимает
    залипшую паузу, история цела, следующий ход не резюмит старую паузу (resume→re-interrupt)."""
    SF = _file_sf
    # durable thread_id с версией в префиксе (checkpoint_ns="" — штатно)
    cfg = {"configurable": {"thread_id": "react-v1:react:t1:hmacZ"}}

    s1 = EncryptedSqlCheckpointSaver(session_factory=SF)
    g1 = _build_interrupt_graph(s1)
    g1.invoke({"messages": [HumanMessage("привет")]}, cfg)  # → interrupt, пауза

    # «рестарт»: новый инстанс saver на той же БД видит паузу (durable)
    s2 = EncryptedSqlCheckpointSaver(session_factory=SF)
    g2 = _build_interrupt_graph(s2)
    snap = g2.get_state(cfg)
    assert snap.next == ("ask",) or (snap.tasks and snap.tasks[0].interrupts), "пауза durable"
    assert snap.values["messages"][0].content == "привет"  # история durable

    # гасим протухшую паузу (checkpoint_ns="" по умолчанию)
    removed = s2.clear_pending("react-v1:react:t1:hmacZ")
    assert removed >= 1

    snap2 = g2.get_state(cfg)
    has_interrupt = bool(snap2.tasks and snap2.tasks and any(t.interrupts for t in snap2.tasks))
    assert not has_interrupt, f"после clear_pending пауза должна исчезнуть, next={snap2.next}"
    assert snap2.values["messages"][0].content == "привет"  # история ЦЕЛА

    # следующий обычный ход — НЕ резюмит старую паузу, продолжает беседу (re-interrupt на новом)
    g2.invoke({"messages": [HumanMessage("ещё")]}, cfg)
    snap3 = g2.get_state(cfg)
    contents = [m.content for m in snap3.values["messages"]]
    assert "привет" in contents and "ещё" in contents  # история накопилась, не сброшена


@pytest.mark.asyncio
async def test_durable_crash_recovery_counter(monkeypatch):
    """ПРАВИЛО #7 — фикс R1 MAJOR (poison-checkpoint recovery): 1й краш durable-треда → clear_pending
    (счётчик=1, тред жив); 2й подряд → delete_thread (poison-сброс); OFF — счётчик не трогается."""
    monkeypatch.setattr(RL, "_persist_enabled", lambda: True)
    RL._DURABLE_CRASH.clear()
    calls = {"clear": 0, "delete": 0}

    class _FakeSaver:
        def clear_pending(self, tid, ns=""):
            calls["clear"] += 1
        def delete_thread(self, tid):
            calls["delete"] += 1

    monkeypatch.setattr(RL, "_get_checkpointer", lambda: _FakeSaver())
    monkeypatch.setattr(RL, "build_slice_tools", lambda *a, **k: [])
    monkeypatch.setattr(RL, "_persist_debug_turn", lambda *a, **k: None)
    # детерминированный краш ВНУТРИ try → except-recovery
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(RL, "_build_graph", _boom)

    kw = dict(session=None, tenant_id="t1", user_id="u1", thread_id="react:t1:crash",
              llm=object(), user_text="привет")
    r1 = await RL.handle_turn(**kw)
    assert calls == {"clear": 1, "delete": 0}  # 1й краш → clear_pending
    assert "потеряла контекст" in str(r1)
    await RL.handle_turn(**kw)
    assert calls == {"clear": 1, "delete": 1}  # 2й подряд → delete_thread
    assert "react-v1:react:t1:crash" not in RL._DURABLE_CRASH  # счётчик сброшен после delete


@pytest.mark.asyncio
async def test_async_wrappers_roundtrip(_file_sf):
    """aput/aget_tuple/aput_writes/alist (to_thread) работают."""
    from langgraph.checkpoint.base import empty_checkpoint

    SF = _file_sf
    saver = EncryptedSqlCheckpointSaver(session_factory=SF)
    cfg = {"configurable": {"thread_id": "react-v1:react:t1:async"}}
    ck = empty_checkpoint()
    ck["id"] = "00000000-0000-0000-0000-0000000000ff"
    ck["channel_values"] = {"messages": [HumanMessage(content="async-привет")]}

    out = await saver.aput(cfg, ck, {"step": 1}, {})
    assert out["configurable"]["checkpoint_id"] == ck["id"]
    await saver.aput_writes({**cfg, "configurable": {**cfg["configurable"], "checkpoint_id": ck["id"]}},
                            [("__interrupt__", "пауза")], task_id="t1")

    got = await saver.aget_tuple(cfg)
    assert got.checkpoint["channel_values"]["messages"][0].content == "async-привет"
    listed = [t async for t in saver.alist(cfg)]
    assert len(listed) == 1
