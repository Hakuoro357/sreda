"""Аудит 2026-07-18 (runtime-react + cross-latency) — регрессионные тесты фиксов react-loop.

Покрытие (номера = находки отчётов):
  #1   порядок гейтов: доступность инструмента РАНЬШЕ time-gates #288/#350;
  #2   confirm-отказ на unified: run_tools метит result_kind="confirm_declined",
       chat-узел НЕ зовёт LLM (детерминированный ответ подставит handle_turn, #321);
  #3   turn_key при пустом inbound_message_id — уникален на ход (трейс треда не глохнет);
  #4   stub-ToolMessage stop-узла — result_kind="step_limit", не считается исполнением;
  #5   prune checkpoint'ов АКТИВНОГО треда (последние N) в EncryptedSqlCheckpointSaver.put;
  #7   clear_pending/delete_thread с tenant-скоупом (NULL-толерантность, legacy/recovery целы);
  #8   RRULE-форма «RRULE:FREQ=…» принимается schedule_reminder;
  FC-1 session.rollback() на tool-failure в dispatch и в catch-all handle_turn;
       флаг _Reply.had_internal_error на catch-all safe-fallback (канал по гарду
       НЕ коммитит inbound «processed» — статус для unprocessed_inbound monitor'а);
  NEW-5 кэш bind_tools на ход: повторный bind того же набора не сериализует схемы заново.

Без сети и без PG (sqlite in-memory / temp file), как у соседних unit-тестов.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop
from sreda.runtime.react_loop import build_slice_tools, handle_turn
from tests.unit.conftest import seed_telegram_user


class _StubLLM:
    """Скриптованный LLM: bind_tools → self; invoke → следующий AIMessage из сценария."""

    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted, self._i = scripted, 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


class _CountBindLLM(_StubLLM):
    """То же + счётчики bind/invoke (для NEW-5 и #2b)."""

    def __init__(self, scripted: list[AIMessage]) -> None:
        super().__init__(scripted)
        self.binds = 0
        self.invokes = 0

    def bind_tools(self, tools):  # noqa: ANN001
        self.binds += 1
        return self

    def invoke(self, messages):  # noqa: ANN001
        self.invokes += 1
        return super().invoke(messages)


def _graph(db_session, u, stub, tools, **kw):
    return react_loop._build_graph(
        stub, tools, tenant_id=u.tenant_id, user_id=u.user_id,
        today_str="2030-01-01", session=db_session, **kw)


def _cfg():
    return {"configurable": {"thread_id": f"t-{uuid4().hex}"}}


# --- #8: RRULE «RRULE:FREQ=…» -------------------------------------------------

def test_schedule_reminder_accepts_rrule_prefixed_form(db_session):
    """#8: RFC-5545 форма с именем свойства («RRULE:FREQ=DAILY») валидна — префикс снимается
    до проверки FREQ=. Раньше модель, послушавшая «RFC-5545 RRULE» из docstring, получала отказ."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    res = tools["schedule_reminder"].invoke({
        "title": "полив", "trigger_iso": "2030-01-01T09:00:00+00:00",
        "recurrence_rule": "RRULE:FREQ=DAILY;COUNT=3"})
    assert res.startswith("ok:scheduled:"), res
    assert "FREQ=DAILY" in res, res


def test_schedule_reminder_rejects_garbage_rrule(db_session):
    """#8 (контраст): мусорное правило по-прежнему отклоняется (strip префикса не ослабил валидацию)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    res = tools["schedule_reminder"].invoke({
        "title": "полив", "trigger_iso": "2030-01-01T09:00:00+00:00",
        "recurrence_rule": "EVERY-DAY-SOMEHOW"})
    assert res.startswith("Не разобрала правило повтора"), res


# --- #1: порядок гейтов --------------------------------------------------------

def test_time_gate_runs_after_availability_gate(db_session):
    """#1: галлюцинированный schedule_reminder на chat-скоупе (инструмент НЕ забинден) получает
    честный unavailable, а НЕ «время не названо» (time_not_specified). Текст юзера без времени —
    старый порядок вернул бы time_not_specified."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = build_slice_tools(db_session, u.tenant_id, u.user_id)
    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "schedule_reminder",
            "args": {"title": "разминка", "trigger_iso": "2030-01-01T09:00:00+00:00"},
            "id": "call_1"}]),
        AIMessage(content="Хорошо."),
    ]
    g = _graph(db_session, u, _StubLLM(scripted), tools, preflight_enabled=True)
    out = g.invoke(
        {"messages": [HumanMessage("привет, расскажи что-нибудь")],
         "intent": "chat", "active_families": [], "turn_key": "tk-g1"},
        _cfg())
    tms = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tms, "ожидали ToolMessage отказа недоступного инструмента"
    rk = (tms[0].artifact or {}).get("result_kind")
    assert rk == "unavailable", f"ожидали unavailable (availability раньше time-gate), got {rk!r}"
    assert rk != "time_not_specified"


# --- #4: stub-ToolMessage stop-узла -------------------------------------------

def test_count_executed_tool_ignores_step_limit_stub():
    """#4: stub stop-узла (result_kind="step_limit") НЕ считается исполнением — как search_limit.
    ok и без-artifact (прежний вид) — по-прежнему считаются."""
    msgs = [
        HumanMessage("поищи"),
        ToolMessage(content="прервано: исчерпан лимит шагов хода", name="web_search",
                    tool_call_id="a", artifact={"result_kind": "step_limit"}),
        ToolMessage(content="результат", name="web_search",
                    tool_call_id="b", artifact={"result_kind": "ok"}),
        ToolMessage(content="legacy без artifact", name="web_search", tool_call_id="c"),
        ToolMessage(content="лимит", name="web_search", tool_call_id="d",
                    artifact={"result_kind": "search_limit"}),
    ]
    assert react_loop._count_executed_tool(msgs, "web_search") == 2


# --- #2: confirm-отказ без LLM-прохода ----------------------------------------

def test_run_tools_marks_confirm_declined_result(db_session):
    """#2: результат-текст отказа confirm-обёртки метится result_kind="confirm_declined"
    (не «ok» — мутация НЕ исполнена; метка нужна трейсу и chat-гейту)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = build_slice_tools(db_session, u.tenant_id, u.user_id)

    def _declined() -> str:
        return "Хорошо, не делаю."

    tools = [StructuredTool.from_function(func=_declined, name="save_core_fact",
                                          description="stub: отказ confirm")
             if t.name == "save_core_fact" else t for t in tools]
    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "save_core_fact", "args": {}, "id": "call_1"}]),
        AIMessage(content="принял."),
    ]
    g = _graph(db_session, u, _StubLLM(scripted), tools)
    out = g.invoke(
        {"messages": [HumanMessage("запомни что-нибудь")],
         "active_families": ["memory"], "turn_key": "tk-d1"},
        _cfg())
    tms = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tms, "ожидали ToolMessage исполненного stub-инструмента"
    assert (tms[0].artifact or {}).get("result_kind") == "confirm_declined", tms[0].artifact


def test_chat_node_skips_llm_on_confirm_declined(db_session):
    """#2: на unified-ходе с последним ToolMessage confirm_declined chat-узел НЕ зовёт LLM:
    возвращает пустой AIMessage (история чистая — без скрытой от юзера галлюцинации),
    llm_calls не пополняется (вызова не было). handle_turn (#321) подставит детерминированный
    ответ — это вне scope данного теста."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = build_slice_tools(db_session, u.tenant_id, u.user_id)
    stub = _CountBindLLM([AIMessage(content="этот текст не должен быть порождён")])
    g = _graph(db_session, u, stub, tools, preflight_enabled=True)
    out = g.invoke(
        {"messages": [
            HumanMessage("удали задачу молоко"),
            AIMessage(content="", tool_calls=[{
                "name": "delete_task", "args": {"task_ref": "task_1"}, "id": "call_1"}]),
            ToolMessage(content="Хорошо, не делаю.", name="delete_task",
                        tool_call_id="call_1",
                        artifact={"result_kind": "confirm_declined"}),
         ],
         "unified_execute": True, "intent": "task",
         "active_families": [], "turn_key": "tk-d2"},
        _cfg())
    assert stub.invokes == 0, "LLM не должен вызываться на детерминированном decline"
    last = out["messages"][-1]
    assert isinstance(last, AIMessage) and last.content == "", (
        f"ожидали пустой AIMessage вместо сочинённого отказа, got {last!r}")
    assert not out.get("llm_calls"), "llm_calls не должен пополняться без вызова"


def test_chat_node_calls_llm_on_confirm_accept_path(db_session):
    """#2 (контраст): обычный результат инструмента (НЕ decline-текст) → chat зовёт LLM как раньше."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = build_slice_tools(db_session, u.tenant_id, u.user_id)
    stub = _CountBindLLM([AIMessage(content="удалила.")])
    g = _graph(db_session, u, stub, tools, preflight_enabled=True)
    out = g.invoke(
        {"messages": [
            HumanMessage("удали задачу молоко"),
            AIMessage(content="", tool_calls=[{
                "name": "delete_task", "args": {"task_ref": "task_1"}, "id": "call_1"}]),
            ToolMessage(content="ok:deleted", name="delete_task",
                        tool_call_id="call_1", artifact={"result_kind": "ok"}),
         ],
         "unified_execute": True, "intent": "task",
         "active_families": [], "turn_key": "tk-d3"},
        _cfg())
    assert stub.invokes == 1, "обычный (не-decline) результат обязан идти через LLM"
    assert out["messages"][-1].content == "удалила."


# --- #3: turn_key при пустом inbound_message_id --------------------------------

@pytest.mark.asyncio
async def test_turn_key_unique_when_inbound_id_empty(db_session, monkeypatch):
    """#3: пустой inbound_message_id (прямые/тестовые вызовы) — turn_key уникален на ход
    (раньше fallback на голый thread_id давал один ключ всем ходам → трейс 2+ ходов глох
    на ON CONFLICT DO NOTHING). Прод-контракт (непустой id) не менялся."""
    captured: list[str] = []
    monkeypatch.setattr(react_loop._trace, "persist_trace_start",
                        lambda **kw: captured.append(kw["turn_key"]))
    u = seed_telegram_user(db_session)
    db_session.commit()
    tid = f"react:test:{uuid4().hex}"
    for _ in range(2):
        await handle_turn(
            session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
            thread_id=tid, llm=_StubLLM([AIMessage(content="ок")]),
            user_text="привет", inbound_message_id="", channel="max")
    assert len(captured) == 2, captured
    assert captured[0] != captured[1], "turn_key ходов обязаны различаться"
    prefix = f"react:max:{u.tenant_id}:{tid}:"
    assert all(k.startswith(prefix) and len(k) > len(prefix) for k in captured), captured


@pytest.mark.asyncio
async def test_turn_key_stable_with_real_inbound_id(db_session, monkeypatch):
    """#3 (контраст): непустой inbound_message_id → прежний формат ключа (byte-identical)."""
    captured: list[str] = []
    monkeypatch.setattr(react_loop._trace, "persist_trace_start",
                        lambda **kw: captured.append(kw["turn_key"]))
    u = seed_telegram_user(db_session)
    db_session.commit()
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:test:{uuid4().hex}", llm=_StubLLM([AIMessage(content="ок")]),
        user_text="привет", inbound_message_id="msg_42", channel="max")
    assert captured == [f"react:max:{u.tenant_id}:msg_42"], captured


# --- FC-1: session.rollback() на tool-failure путях -----------------------------

def test_tool_failure_rolls_back_shared_session(db_session):
    """FC-1 (dispatch): исключение инструмента → session.rollback() ДО error-ToolMessage —
    shared session не остаётся в failed-state (иначе следующий инструмент батча/прохода
    упал бы PendingRollbackError)."""
    from unittest.mock import MagicMock

    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = build_slice_tools(db_session, u.tenant_id, u.user_id)

    def _boom() -> str:
        raise RuntimeError("db is down")

    tools = [StructuredTool.from_function(func=_boom, name="list_reminders",
                                          description="stub: падающий инструмент")
             if t.name == "list_reminders" else t for t in tools]
    mock_session = MagicMock()
    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "list_reminders", "args": {}, "id": "call_1"}]),
        AIMessage(content="не вышло."),
    ]
    g = react_loop._build_graph(
        _StubLLM(scripted), tools, tenant_id=u.tenant_id, user_id=u.user_id,
        today_str="2030-01-01", session=mock_session, persona_overlay="")
    out = g.invoke(
        {"messages": [HumanMessage("покажи дела")],
         "active_families": [], "turn_key": "tk-f1"},
        _cfg())
    assert mock_session.rollback.called, "dispatch обязан откатить shared session после сбоя"
    tms = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tms and tms[0].status == "error"
    assert (tms[0].artifact or {}).get("result_kind") == "error"


@pytest.mark.asyncio
async def test_handle_turn_catchall_rolls_back_session():
    """FC-1 (catch-all): краш хода (LLM недоступен) → session.rollback() в except handle_turn —
    post-turn commit call-site'а не упадёт PendingRollbackError."""
    from unittest.mock import MagicMock

    class _BoomLLM:
        def bind_tools(self, tools):  # noqa: ANN001
            return self

        def invoke(self, messages):  # noqa: ANN001
            raise TimeoutError("llm down")

    mock_session = MagicMock()
    reply = await handle_turn(
        session=mock_session, tenant_id="tenant_fc1", user_id="user_fc1",
        thread_id=f"react:test:{uuid4().hex}", llm=_BoomLLM(),
        user_text="привет", inbound_message_id="m1", channel="max")
    assert mock_session.rollback.called, "catch-all обязан откатить shared session"
    assert "потеряла контекст" in str(reply) or "подвела" in str(reply), str(reply)


# --- FC-1: флаг _Reply.had_internal_error на safe-fallback ----------------------

@pytest.mark.asyncio
async def test_handle_turn_catchall_sets_had_internal_error():
    """FC-1 (флаг): safe-fallback catch-all помечается had_internal_error=True — канал
    (telegram_inbound/max_inbound, гард getattr(_reply, 'had_internal_error', False))
    НЕ коммитит inbound «processed»: статус остаётся для unprocessed_inbound monitor'а."""
    from unittest.mock import MagicMock

    class _BoomLLM:
        def bind_tools(self, tools):  # noqa: ANN001
            return self

        def invoke(self, messages):  # noqa: ANN001
            raise TimeoutError("llm down")

    reply = await handle_turn(
        session=MagicMock(), tenant_id="tenant_fc1f", user_id="user_fc1f",
        thread_id=f"react:test:{uuid4().hex}", llm=_BoomLLM(),
        user_text="привет", inbound_message_id="m1", channel="max")
    assert reply.had_internal_error is True
    # ровно та форма гарда, что стоит в каналах:
    assert getattr(reply, "had_internal_error", False) is True


@pytest.mark.asyncio
async def test_handle_turn_success_reply_has_no_internal_error(db_session):
    """FC-1 (контраст): успешный ход НЕ помечен — гард канала пропускает commit «processed»."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    reply = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:test:{uuid4().hex}", llm=_StubLLM([AIMessage(content="ок")]),
        user_text="привет", inbound_message_id="", channel="max")
    assert str(reply) == "ок"
    assert reply.had_internal_error is False
    assert not getattr(reply, "had_internal_error", False)


def test_reply_default_had_internal_error_false():
    """FC-1 (дефолт): старые точки конструирования _Reply (pause/no-op/финал хода) —
    флаг False; поведение getattr-гардов канала на них не меняется."""
    r = react_loop._Reply("текст", awaiting_confirm=True, confirm_id="p1")
    assert str(r) == "текст"
    assert r.awaiting_confirm is True and r.confirm_id == "p1"
    assert r.had_internal_error is False
    assert react_loop._Reply("").had_internal_error is False  # no-op resume_only


# --- NEW-5: кэш bind_tools на ход ----------------------------------------------

def test_bind_tools_cached_within_turn(db_session):
    """NEW-5: на типовом ходе (несколько итераций с неизменным bound) bind_tools вызывается
    ОДИН раз на (llm, набор имён) — повторная сериализация ~49 схем на итерацию снята."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = build_slice_tools(db_session, u.tenant_id, u.user_id)
    stub = _CountBindLLM([
        AIMessage(content="", tool_calls=[{
            "name": "list_reminders", "args": {}, "id": "call_1"}]),
        AIMessage(content="", tool_calls=[{
            "name": "list_reminders", "args": {}, "id": "call_2"}]),
        AIMessage(content="готово."),
    ])
    g = _graph(db_session, u, stub, tools, persona_overlay="")
    out = g.invoke(
        {"messages": [HumanMessage("покажи дела")],
         "active_families": [], "turn_key": "tk-b1"},
        _cfg())
    assert out["messages"][-1].content == "готово."
    assert stub.invokes == 3, f"ожидали 3 chat-прохода, got {stub.invokes}"
    assert stub.binds == 1, f"bound не менялся — bind обязан быть кэширован, got {stub.binds}"


# --- #5/#7: checkpoint-saver (sqlite temp-file, как у соседнего теста #193) ----

@pytest.fixture()
def chk_db():
    import os
    import tempfile

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SF, engine
    engine.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


def _ckpt_cfg(thread="react:t1:hmacX", ns="react-v1", cp_id=None):
    conf = {"thread_id": thread, "checkpoint_ns": ns}
    if cp_id is not None:
        conf["checkpoint_id"] = cp_id
    return {"configurable": conf}


def _ckpt(cp_id, text_content="напомни купить молоко"):
    from langgraph.checkpoint.base import empty_checkpoint

    ck = empty_checkpoint()
    ck["id"] = cp_id
    ck["channel_values"] = {"messages": [HumanMessage(content=text_content)]}
    return ck


def _cp_id(i: int) -> str:
    return f"00000000-0000-0000-0000-0000000000{i:02d}"


def test_put_prunes_thread_to_last_n_checkpoints(chk_db):
    """#5/NEW-3: put оставляет последние PRUNE_KEEP_PER_THREAD checkpoint'ов на тред —
    АКТИВНЫЙ тред не растёт бессрочно (retention-GC режет только неактивные >30д)."""
    from sreda.runtime import react_checkpoint_saver as rcs

    SF, _ = chk_db
    saver = rcs.EncryptedSqlCheckpointSaver(session_factory=SF, owner_session_factory=SF)
    total = rcs.PRUNE_KEEP_PER_THREAD + 5
    for i in range(1, total + 1):
        saver.put(_ckpt_cfg(), _ckpt(_cp_id(i), f"сообщение {i}"), {"step": i}, {})

    from sreda.db.models.react_checkpoint import ReactCheckpoint
    with SF() as s:
        ids = {r[0] for r in s.execute(
            __import__("sqlalchemy").select(ReactCheckpoint.checkpoint_id)).all()}
    assert len(ids) == rcs.PRUNE_KEEP_PER_THREAD, ids
    # выжили ровно последние N (максимальные id)
    assert ids == {_cp_id(i) for i in range(6, total + 1)}, sorted(ids)
    # hot-path цел: последний checkpoint читается
    got = saver.get_tuple(_ckpt_cfg())
    assert got is not None and got.checkpoint["id"] == _cp_id(total)


def test_put_prune_disabled_when_keep_nonpositive(chk_db, monkeypatch):
    """#5 (kill-switch): PRUNE_KEEP_PER_THREAD=0 → обрезка OFF (байт-идентичный откат)."""
    from sreda.runtime import react_checkpoint_saver as rcs

    monkeypatch.setattr(rcs, "PRUNE_KEEP_PER_THREAD", 0)
    SF, _ = chk_db
    saver = rcs.EncryptedSqlCheckpointSaver(session_factory=SF, owner_session_factory=SF)
    for i in range(1, 4):
        saver.put(_ckpt_cfg(), _ckpt(_cp_id(i)), {"step": i}, {})

    from sreda.db.models.react_checkpoint import ReactCheckpoint
    with SF() as s:
        n = s.execute(__import__("sqlalchemy").select(
            __import__("sqlalchemy").func.count()).select_from(ReactCheckpoint)).scalar()
    assert n == 3


def test_clear_pending_tenant_scoped(chk_db):
    """#7: clear_pending с чужим tenant_id — no-op; со своим — гасит паузу (interrupt idx<0)."""
    from sreda.db.session import tenant_ctx
    from sreda.runtime import react_checkpoint_saver as rcs

    SF, _ = chk_db
    saver = rcs.EncryptedSqlCheckpointSaver(session_factory=SF, owner_session_factory=SF)
    tok = tenant_ctx.set("tenant_one")
    try:
        ck = _ckpt(_cp_id(1))
        saver.put(_ckpt_cfg(), ck, {"step": 1}, {})
        saver.put_writes(_ckpt_cfg(cp_id=ck["id"]),
                         [("messages", "v0"), ("__interrupt__", "пауза")], task_id="task1")
    finally:
        tenant_ctx.reset(tok)

    assert saver.clear_pending(_ckpt_cfg()["configurable"]["thread_id"],
                               _ckpt_cfg()["configurable"]["checkpoint_ns"],
                               tenant_id="tenant_two") == 0, "чужой tenant не должен ничего гасить"
    assert saver.clear_pending(_ckpt_cfg()["configurable"]["thread_id"],
                               _ckpt_cfg()["configurable"]["checkpoint_ns"],
                               tenant_id="tenant_one") > 0, "свой tenant обязан погасить паузу"


def test_delete_thread_tenant_scoped_and_null_tolerant(chk_db):
    """#7: delete_thread с чужим tenant_id не трогает тред; со своим удаляет; легаси NULL-штамп
    (запись без tenant_ctx) удаляется любым caller'ом — poison-recovery не ломается."""
    from sreda.db.session import tenant_ctx
    from sreda.runtime import react_checkpoint_saver as rcs

    SF, _ = chk_db
    saver = rcs.EncryptedSqlCheckpointSaver(session_factory=SF, owner_session_factory=SF)
    tok = tenant_ctx.set("tenant_one")
    try:
        saver.put(_ckpt_cfg("react:t1:x"), _ckpt(_cp_id(1)), {"step": 1}, {})
    finally:
        tenant_ctx.reset(tok)

    saver.delete_thread("react:t1:x", tenant_id="tenant_two")
    assert saver.get_tuple(_ckpt_cfg("react:t1:x")) is not None, "чужой tenant снёс тред!"
    saver.delete_thread("react:t1:x", tenant_id="tenant_one")
    assert saver.get_tuple(_ckpt_cfg("react:t1:x")) is None, "свой tenant не смог удалить тред"

    saver.put(_ckpt_cfg("react:t2:x"), _ckpt(_cp_id(2)), {"step": 1}, {})  # без ctx → NULL-штамп
    saver.delete_thread("react:t2:x", tenant_id="tenant_one")
    assert saver.get_tuple(_ckpt_cfg("react:t2:x")) is None, "NULL-штамп (легаси) обязан удаляться"
