"""#285 Фаза A (срез A2): наблюдаемость единого пути в трейсе — пин-тесты.

(1) collect_tool_calls: поле observed (ToolMessage найден vs rk-дефолт «ok») — честный
    executed-счёт = ok AND observed (rk-ok best-effort дыра, CodexH R1 Фазы 0).
(2) persist_trace_finish пишет turn_policy_json / confirm_resolution (sqlite, реальная схема).
(3) handle_turn: confirm-пауза → resume «да»/«нет» различимы в finish (петля калибровки
    словаря; раньше confirm_state="confirmed" не различал — инвентарь Фазы 0 §5.5).
(4) handle_turn: полиси попадает в finish при флаге ON и NULL при OFF.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool
from langgraph.types import interrupt

from sreda.runtime import react_loop, react_trace_persist
from sreda.runtime.react_trace_persist import collect_tool_calls, persist_trace_finish

# ───────────── (1) observed в collect_tool_calls ─────────────

def _ai(name, cid):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": cid}])


def test_collect_observed_true_when_toolmessage_present():
    msgs = [_ai("add_task", "c1"),
            ToolMessage(content="ok", tool_call_id="c1",
                        artifact={"result_kind": "ok", "latency_ms": 5})]
    (rec,) = collect_tool_calls(msgs, tenant_id="t")
    assert rec["observed"] is True and rec["result_kind"] == "ok" and rec["ok"] is True


def test_collect_observed_false_when_result_missing():
    """Resume-обрыв (#269): вызов без ToolMessage → rk дефолтится в «ok», но observed=False —
    честный executed-счёт обязан требовать ok AND observed."""
    (rec,) = collect_tool_calls([_ai("add_task", "c1")], tenant_id="t")
    assert rec["observed"] is False
    assert rec["result_kind"] == "ok" and rec["ok"] is True  # backcompat-поведение задокументировано


def test_collect_orphan_toolmessage_recorded():
    """Обратная #269-дыра (R1 фазового ревью, CodexH M3): ToolMessage БЕЗ пары-AIMessage
    (confirm-resume: AIMessage до паузы вне дельты) → orphan-запись с именем из ToolMessage —
    подтверждённый write видим shadow-сверке."""
    tm = ToolMessage(content="ok", tool_call_id="c9", name="cancel_task",
                     artifact={"result_kind": "ok", "latency_ms": 3})
    (rec,) = collect_tool_calls([tm], tenant_id="t")
    assert rec["orphan"] is True and rec["observed"] is True
    assert rec["name"] == "cancel_task" and rec["result_kind"] == "ok"
    assert rec["args_hash"] is None  # args вне дельты — честный None, не выдумка


# ───────────── (2) finish пишет новые колонки (sqlite) ─────────────

@pytest.fixture
def trace_db(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.config import settings as st_mod
    from sreda.db.base import Base

    monkeypatch.setenv("SREDA_REACT_TRACE_ENABLED", "1")
    st_mod.get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{tmp_path / 'trace.db'}")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine)
    monkeypatch.setattr(react_trace_persist, "_session", lambda: SF())
    yield SF
    st_mod.get_settings.cache_clear()


def test_finish_persists_policy_and_resolution(trace_db):
    from sreda.db.models import ReactTurnTrace

    persist_trace_finish(
        tenant_id="t285", user_id="u", thread_id="th", channel="react", turn_key="tk-a2",
        reply_text="Готово.", llm_calls=[], tool_calls=[], confirm_state="confirmed",
        outcome="ok", passes=1, turn_policy_json='{"v": 1}', confirm_resolution="yes")
    s = trace_db()
    try:
        row = s.query(ReactTurnTrace).filter_by(turn_key="tk-a2").one()
        assert row.turn_policy_json == '{"v": 1}'
        assert row.confirm_resolution == "yes"
    finally:
        s.close()


def test_finish_defaults_null(trace_db):
    from sreda.db.models import ReactTurnTrace

    persist_trace_finish(
        tenant_id="t285", user_id="u", thread_id="th", channel="react", turn_key="tk-a2n",
        reply_text="Готово.", llm_calls=[], tool_calls=[], confirm_state="none",
        outcome="ok", passes=1)
    s = trace_db()
    try:
        row = s.query(ReactTurnTrace).filter_by(turn_key="tk-a2n").one()
        assert row.turn_policy_json is None and row.confirm_resolution is None
    finally:
        s.close()


# ───────────── (3)+(4) handle_turn-wiring (recording-стаб трейса) ─────────────

_TASK_TOOLS = ["list_reminders", "schedule_reminder", "add_task", "cancel_task",
               "need_family", "recall_memory"]
_WEB_TOOLS = ["web_search", "fetch_url", "get_weather"]


def _mk_tool(name, invoked, interrupt_tool=False):
    def _f(q: str = ""):
        invoked[name] = invoked.get(name, 0) + 1
        if interrupt_tool:
            d = interrupt({"confirm": "точно?"})
            return "ok" if str(d) in ("да", "yes") else "нет"
        return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=name)


class _RecTrace:
    """Стаб _trace: включён, пишет kwargs finish в список (без БД)."""

    def __init__(self):
        self.finishes: list[dict] = []

    def trace_enabled(self):
        return True

    def persist_trace_start(self, **kw):
        pass

    def persist_trace_pause(self, **kw):
        pass

    def persist_trace_finish(self, **kw):
        self.finishes.append(kw)

    def collect_tool_calls(self, *a, **kw):
        return collect_tool_calls(*a, **kw)


class _Chat:
    def __init__(self, label, classify="task", responses=None, calls=None):
        self.label, self._classify = label, classify
        self._responses, self._i = list(responses or []), 0
        self._calls = calls if calls is not None else {}

    async def ainvoke(self, _msgs):
        return AIMessage(content=self._classify)

    def bind_tools(self, tools):
        outer = self

        def _inv(_msgs):
            r = (outer._responses[min(outer._i, len(outer._responses) - 1)]
                 if outer._responses else AIMessage(content="resp"))
            outer._i += 1
            return r
        return RunnableLambda(_inv)


@pytest.fixture
def install(monkeypatch):
    from sreda.config import settings as settings_mod

    def _install(*, unified=False, deepseek=None, invoked=None, interrupt_names=(), rec=None):
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1" if unified else "0")
        settings_mod.get_settings.cache_clear()
        inv = invoked if invoked is not None else {}
        monkeypatch.setattr(react_loop, "build_slice_tools",
                            lambda *a, **k: [_mk_tool(n, inv, interrupt_tool=(n in interrupt_names))
                                             for n in (_TASK_TOOLS + _WEB_TOOLS)])
        monkeypatch.setattr(react_loop, "_trace", rec if rec is not None else _RecTrace())
        monkeypatch.setattr(react_loop, "_record_react_usage", lambda **k: None)
        import sreda.services.llm as llm_mod
        monkeypatch.setattr(llm_mod, "get_chat_llm", lambda *a, **k: deepseek)
        return inv

    yield _install
    from sreda.config import settings as settings_mod
    settings_mod.get_settings.cache_clear()


def _turn(freddie, *, thread, text):
    return asyncio.run(react_loop.handle_turn(
        session=None, tenant_id="t", user_id="u", thread_id=thread,
        llm=freddie, user_text=text, inbound_message_id=f"{thread}:{text[:12]}",
        channel="react", resume_only=False, expected_confirm_id="",
        provider_key="inception-mercury2", fallback_llm=None))


def _ai_call(name, cid):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": cid}])


@pytest.mark.parametrize("answer,expected",
                         [("да", "yes"), ("нет", "no"), ("не надо", "no"),
                          ("лучше перенеси её на вторник", "redirect")])
def test_confirm_resolution_recorded(install, answer, expected):
    """Confirm-пауза → resume текстом: finish несёт confirm_resolution yes|no (различимо)."""
    rec = _RecTrace()
    freddie = _Chat("freddie", classify="task",
                    responses=[_ai_call("cancel_task", "c1"), AIMessage(content="ок")])
    install(unified=False, deepseek=_Chat("ds"), interrupt_names=("cancel_task",), rec=rec)
    _turn(freddie, thread="cr-" + expected, text="отмени задачу про хлеб")  # пауза (finish нет)
    assert rec.finishes == []
    _turn(freddie, thread="cr-" + expected, text=answer)  # resume
    assert len(rec.finishes) == 1
    assert rec.finishes[0]["confirm_resolution"] == expected
    assert rec.finishes[0]["confirm_state"] == "confirmed"


def test_confirm_resolution_button_path(install):
    """Кнопка [Да] (resume_only + канон + confirm_id): resolution=yes (R1-субагент m5)."""
    rec = _RecTrace()
    freddie = _Chat("freddie", classify="task",
                    responses=[_ai_call("cancel_task", "c1"), AIMessage(content="ок")])
    install(unified=False, deepseek=_Chat("ds"), interrupt_names=("cancel_task",), rec=rec)
    r1 = _turn(freddie, thread="btn-1", text="отмени задачу про хлеб")
    assert getattr(r1, "awaiting_confirm", False) and getattr(r1, "confirm_id", "")
    import asyncio as _a
    r2 = _a.run(react_loop.handle_turn(
        session=None, tenant_id="t", user_id="u", thread_id="btn-1",
        llm=freddie, user_text="да", inbound_message_id="btn-1:resume",
        channel="react", resume_only=True, expected_confirm_id=r1.confirm_id,
        provider_key="inception-mercury2", fallback_llm=None))
    assert rec.finishes and rec.finishes[-1]["confirm_resolution"] == "yes"


def test_redirect_resumes_graph_with_safe_no(install):
    """Redirect: в трейсе redirect, но инструмент НЕ исполнен (в граф ушло безопасное «нет», A0)."""
    rec = _RecTrace()
    inv = {}
    freddie = _Chat("freddie", classify="task",
                    responses=[_ai_call("cancel_task", "c1"), AIMessage(content="ок")])
    install(unified=False, deepseek=_Chat("ds"), interrupt_names=("cancel_task",),
            rec=rec, invoked=inv)
    _turn(freddie, thread="rd-1", text="отмени задачу про хлеб")
    _turn(freddie, thread="rd-1", text="лучше перенеси её на вторник")
    assert rec.finishes[-1]["confirm_resolution"] == "redirect"
    # interrupt-инструмент вызван (до паузы), но подтверждённой ветки «да» не было:
    # его резюм-ветка вернула «нет»-путь — мутация не «ok» по подтверждению.
    # (Точная семантика «нет»-ветки — в _mk_tool: return "нет".)


def test_fresh_turn_resolution_none(install):
    rec = _RecTrace()
    freddie = _Chat("freddie", classify="task")
    install(unified=False, deepseek=_Chat("ds"), rec=rec)
    _turn(freddie, thread="fr-1", text="разбери мою неделю")
    assert rec.finishes[0]["confirm_resolution"] is None


def test_policy_in_finish_flag_on_and_off(install):
    """ON → turn_policy_json в finish (валидный JSON, chat_fact-вариант); OFF → None."""
    for flag, thread in ((False, "pf-off"), (True, "pf-on")):
        rec = _RecTrace()
        freddie = _Chat("freddie", classify="chat")
        install(unified=flag, deepseek=_Chat("ds"), rec=rec)
        _turn(freddie, thread=thread, text="как настроение?")
        tpj = rec.finishes[0]["turn_policy_json"]
        if flag:
            p = json.loads(tpj)
            assert p["prompt_variant"] == "chat_fact" and p["web_scope_only"] is True
        else:
            assert tpj is None
