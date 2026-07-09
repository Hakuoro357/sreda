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
    raw_chat = "40921122"
    base = f"react:tenant_max_{raw_chat}:{raw_chat}"  # в Среде account id сидит И в tenant
    cfg = RL._build_thread_config(base, 2)
    tid = cfg["configurable"]["thread_id"]
    # версия в ПРЕФИКСЕ (checkpoint_ns зарезервирован LangGraph под подграфы); gen НЕ в ключе
    assert tid.startswith("react-v1:")
    assert "checkpoint_ns" not in cfg["configurable"]
    # чеклист #193 п.2: весь идентификатор хеширован — ни chat_id, ни tenant-сегмент не плейнтекстом
    assert raw_chat not in tid
    assert "tenant_max" not in tid
    assert len(tid) == len("react-v1:") + 64  # sha256 hexdigest
    # детерминирован (тот же base → тот же ключ → durable переживает рестарт)
    assert RL._durable_thread_id(base) == tid
    assert isinstance(RL._get_checkpointer(), EncryptedSqlCheckpointSaver)


def test_durable_key_hides_raw_chat_id(_persist_on):
    """ПРАВИЛО #7 чеклист #193 п.2: account id (chat_id, и в tenant) в durable-ключе только HMAC."""
    raw_chat = "987654321"
    tid = RL._durable_thread_id(f"react:tenant_max_{raw_chat}:{raw_chat}")
    assert raw_chat not in tid  # ни в chat-, ни в tenant-сегменте — весь base хеширован
    # разные base → разные ключи; одинаковые → одинаковые (детерминизм для durable)
    assert RL._durable_thread_id("react:t:111") != RL._durable_thread_id("react:t:222")
    assert RL._durable_thread_id("react:t:111") == RL._durable_thread_id("react:t:111")


def test_durable_key_fail_closed_without_encryption_key(monkeypatch):
    """Fail-closed (CR hmac MEDIUM): без encryption_key durable НЕ строит guessable-ключ → исключение."""
    monkeypatch.setattr(RL, "_persist_enabled", lambda: True)
    monkeypatch.setenv("SREDA_REACT_PERSIST_ENABLED", "1")
    monkeypatch.delenv("SREDA_ENCRYPTION_KEY", raising=False)
    st_mod.get_settings.cache_clear()
    from sreda.services.encryption import EncryptionConfigError
    with pytest.raises(EncryptionConfigError):
        RL._durable_thread_id("react:t:1")
    st_mod.get_settings.cache_clear()


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
    assert RL._durable_thread_id("react:t1:crash") not in RL._DURABLE_CRASH  # счётчик сброшен после delete


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


@pytest.mark.asyncio
async def test_durable_transient_llm_crash_preserves_thread_225(monkeypatch):
    """#225: N подряд ТРАНЗИЕНТНЫХ LLM-сбоев (LLMCallTimeout) → delete_thread НЕ зван, история цела
    (poison-счётчик НЕ копится); ответ НЕ врёт «потеряла контекст». Транзиент ≠ porча стейта."""
    from sreda.services.llm import LLMCallTimeout
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

    def _boom_transient(*a, **k):
        raise LLMCallTimeout("LLM invoke exceeded wall time")
    monkeypatch.setattr(RL, "_build_graph", _boom_transient)

    kw = dict(session=None, tenant_id="t1", user_id="u1", thread_id="react:t1:transient225",
              llm=object(), user_text="привет")
    await RL.handle_turn(**kw)
    await RL.handle_turn(**kw)
    r3 = await RL.handle_turn(**kw)  # три подряд транзиента
    assert calls["delete"] == 0, f"#225: транзиент НЕ должен сносить тред, delete={calls['delete']}"
    assert calls["clear"] == 3, f"#225: каждый транзиент → clear_pending, clear={calls['clear']}"
    assert RL._durable_thread_id("react:t1:transient225") not in RL._DURABLE_CRASH  # счётчик не копился
    assert "потеряла контекст" not in str(r3), f"#225: на транзиенте не врать про потерю: {r3}"


def test_is_transient_llm_exc_classifier_225():
    """#225 классификатор: транзиентные → True, porча-shaped → False, транзиент в __context__ (не __cause__)
    при non-matching __cause__ → True (DFS по обоим, R1 MINOR high). Калибровка-страж (R1 субагент MINOR)."""
    from sreda.services.llm import LLMCallTimeout
    f = RL._is_transient_llm_exc
    # транзиентные
    assert f(LLMCallTimeout("wall")) is True
    assert f(ConnectionError("net")) is True
    assert f(TimeoutError("t")) is True

    class RateLimitError(Exception):
        pass
    assert f(RateLimitError("429")) is True  # по имени
    # порча-стейта / generic → НЕ транзиент (poison-путь цел)
    for exc in (RuntimeError("boom"), ValueError("v"), KeyError("k"), TypeError("ty"), AttributeError("a")):
        assert f(exc) is False, exc

    class BadRequestError(Exception):  # провайдерская ПОСТОЯННАЯ (porча под ней) → НЕ транзиент
        pass
    BadRequestError.__module__ = "openai"
    assert f(BadRequestError("bad")) is False
    # транзиент ТОЛЬКО в __context__, а __cause__ — non-matching (DFS по обоим)
    wrapper = RuntimeError("wrap")
    wrapper.__cause__ = ValueError("permanent")
    wrapper.__context__ = ConnectionError("net down")
    assert f(wrapper) is True
    # R2 (субагент MINOR, анти-#74): лочим РЕАЛЬНЫЕ openai-имена — ап SDK с переименованием транзиентного
    # класса покраснит этот тест, а не молча начнёт вытирать беседу на egress-down.
    openai = pytest.importorskip("openai")
    for nm in ("APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError"):
        cls = getattr(openai, nm, None)
        if cls is not None:
            assert f(cls.__new__(cls)) is True, f"openai.{nm} должен быть транзиентом"
    for nm in ("BadRequestError", "AuthenticationError", "PermissionDeniedError", "NotFoundError"):
        cls = getattr(openai, nm, None)
        if cls is not None:
            assert f(cls.__new__(cls)) is False, f"openai.{nm} (постоянная) → poison-путь, НЕ транзиент"


@pytest.mark.asyncio
async def test_durable_badrequest_still_deletes_225(monkeypatch):
    """#225 (R1 high MAJOR): porча под provider-ошибкой (BadRequestError, module=openai) — НЕ транзиент →
    poison-путь цел: 2 подряд → delete_thread (юзер не залипает навсегда)."""
    monkeypatch.setattr(RL, "_persist_enabled", lambda: True)
    RL._DURABLE_CRASH.clear()
    calls = {"clear": 0, "delete": 0}

    class _FakeSaver:
        def clear_pending(self, tid, ns=""):
            calls["clear"] += 1
        def delete_thread(self, tid):
            calls["delete"] += 1

    class BadRequestError(Exception):
        pass
    BadRequestError.__module__ = "openai"
    monkeypatch.setattr(RL, "_get_checkpointer", lambda: _FakeSaver())
    monkeypatch.setattr(RL, "build_slice_tools", lambda *a, **k: [])
    monkeypatch.setattr(RL, "_build_graph", lambda *a, **k: (_ for _ in ()).throw(BadRequestError("bad")))
    kw = dict(session=None, tenant_id="t1", user_id="u1", thread_id="react:t1:badreq225",
              llm=object(), user_text="привет")
    await RL.handle_turn(**kw)
    await RL.handle_turn(**kw)
    assert calls["delete"] == 1, f"#225: porча-под-BadRequest должна сноситься на 2-м, delete={calls['delete']}"


@pytest.mark.asyncio
async def test_durable_transient_resets_poison_counter_225(monkeypatch):
    """#225 (R1 medium MAJOR): транзиент РВЁТ цепочку «подряд» → poison→transient→poison НЕ сносит
    (крахи не подряд-poison). Счётчик сбрасывается на транзиенте."""
    from sreda.services.llm import LLMCallTimeout
    monkeypatch.setattr(RL, "_persist_enabled", lambda: True)
    RL._DURABLE_CRASH.clear()
    calls = {"clear": 0, "delete": 0}

    class _FakeSaver:
        def clear_pending(self, tid, ns=""):
            calls["clear"] += 1
        def delete_thread(self, tid):
            calls["delete"] += 1

    seq = [RuntimeError("boom"), LLMCallTimeout("net"), RuntimeError("boom")]
    idx = {"n": 0}

    def _boom_seq(*a, **k):
        e = seq[idx["n"]]
        idx["n"] += 1
        raise e
    monkeypatch.setattr(RL, "_get_checkpointer", lambda: _FakeSaver())
    monkeypatch.setattr(RL, "build_slice_tools", lambda *a, **k: [])
    monkeypatch.setattr(RL, "_build_graph", _boom_seq)
    kw = dict(session=None, tenant_id="t1", user_id="u1", thread_id="react:t1:mixed225",
              llm=object(), user_text="привет")
    await RL.handle_turn(**kw)  # poison → counter=1
    await RL.handle_turn(**kw)  # transient → reset
    await RL.handle_turn(**kw)  # poison → counter=1 (не 2)
    assert calls["delete"] == 0, f"#225: транзиент между poison-крахами рвёт «подряд», delete={calls['delete']}"
