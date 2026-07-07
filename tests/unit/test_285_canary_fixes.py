"""#285 фиксы канарейки (инцидент 2026-07-06 на tenant_max_40921122).

Канарейка единого пути показала: политика/скоуп/бинд КОРРЕКТНЫ, но слои поверх ломали ход.
- #1 директива route_domains на едином пути называла инструмент домена, который политика НЕ
  разрешила («как дела?» → primary=checklists → «зови list_checklists», а политика web-only) →
  модель звала незабинженный тул → domain_blocked в цикле → «какой чеклист?».
- #3 не было детекта петли (жгло до 8 проходов) + сообщение блокировки не говорило «не зови снова».

Фиксы гейтед на unified_execute (легаси байт-идентичен). Тесты — юнит + сквозь handle_turn.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop

_TASK_TOOLS = ["list_reminders", "schedule_reminder", "add_task", "cancel_task",
               "list_checklists", "need_family", "recall_memory"]
_WEB_TOOLS = ["web_search", "fetch_url", "get_weather"]


def _ai_call(name, cid, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def _mk_tool(name, invoked):
    def _f(q: str = "", **kw):
        invoked[name] = invoked.get(name, 0) + 1
        return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=name)


class _NoTrace:
    def __getattr__(self, _):
        return lambda *a, **k: None


class _Chat:
    def __init__(self, label, *, msgs_capture=None, responses=None):
        self.label, self._mcap = label, msgs_capture
        self._responses, self._i = list(responses or []), 0

    async def ainvoke(self, _msgs):
        return AIMessage(content="chat")

    def bind_tools(self, tools):
        outer = self

        def _inv(_msgs):
            if outer._mcap is not None:
                outer._mcap.append(list(_msgs))
            if outer._responses:
                r = outer._responses[min(outer._i, len(outer._responses) - 1)]
                outer._i += 1
                return r
            return AIMessage(content="resp-" + outer.label)
        return RunnableLambda(_inv)


@pytest.fixture
def install(monkeypatch):
    from sreda.config import settings as settings_mod

    def _install(*, unified_flag=True, unified_tenants="t", deepseek=None):
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1" if unified_flag else "0")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_TENANTS", unified_tenants)
        settings_mod.get_settings.cache_clear()
        inv = {}
        monkeypatch.setattr(react_loop, "build_slice_tools", lambda *a, **k: [
            _mk_tool(n, inv) for n in (_TASK_TOOLS + _WEB_TOOLS)])
        monkeypatch.setattr(react_loop, "_trace", _NoTrace())
        monkeypatch.setattr(react_loop, "_persist_debug_turn", lambda **k: None)
        monkeypatch.setattr(react_loop, "_record_react_usage", lambda **k: None)
        import sreda.services.llm as llm_mod
        monkeypatch.setattr(llm_mod, "get_chat_llm", lambda *a, **k: deepseek)
        return inv

    yield _install
    settings_mod.get_settings.cache_clear()


def _turn(freddie, *, thread, text, tenant="t"):
    return asyncio.run(react_loop.handle_turn(
        session=None, tenant_id=tenant, user_id="u", thread_id=thread,
        llm=freddie, user_text=text, inbound_message_id=f"{thread}:{text[:10]}",
        channel="react", resume_only=False, expected_confirm_id="",
        provider_key="inception-mercury2", fallback_llm=None))


def _last_human_text(msgs):
    hs = [m for m in msgs if isinstance(m, HumanMessage)]
    return str(hs[-1].content) if hs else ""


def _flat(msgs):
    return "\n".join(str(getattr(m, "content", "")) for m in msgs)


# ─────────── юнит: _domain_blocked_count ───────────
def _tm(kind):
    return ToolMessage(content="x", tool_call_id="c", name="t",
                       artifact={"result_kind": kind})


def test_domain_blocked_count_basic():
    assert react_loop._domain_blocked_count([]) == 0
    assert react_loop._domain_blocked_count(None) == 0
    msgs = [HumanMessage(content="q"), _tm("domain_blocked"), _tm("ok"), _tm("domain_blocked")]
    assert react_loop._domain_blocked_count(msgs) == 2


def test_domain_blocked_count_stops_at_last_human():
    # блоки ПРОШЛОГО хода (до последнего human) не считаются — счёт только текущего хода
    msgs = [_tm("domain_blocked"), _tm("domain_blocked"),
            HumanMessage(content="новый ход"), _tm("domain_blocked")]
    assert react_loop._domain_blocked_count(msgs) == 1


# ─────────── #1: директива гейтится по allowed-домену на unified ───────────
def test_smalltalk_suppresses_checklist_directive(install):
    """«как дела?»: route-мина даёт checklists-директиву, но политика по идиоме → web-only.
    На едином пути директива ПОДАВЛЕНА (не толкает в незабинженный list_checklists)."""
    from sreda.runtime.react_preflight import route_domains
    _rr = route_domains("как дела?")
    assert _rr.directive, "премиса: route-мина «как дела?» даёт директиву"  # sanity
    assert _rr.primary_domain not in ("web",), _rr.primary_domain          # это own-data домен
    msgs = []
    install(deepseek=_Chat("ds"))
    _turn(_Chat("freddie", msgs_capture=msgs), thread="cf1", text="как дела?")
    assert msgs, "chat должен был вызваться"
    assert _rr.directive not in _last_human_text(msgs[-1]), \
        "директива запрещённого домена не должна попасть в хвост на «как дела?»"


def test_allowed_domain_directive_kept(install):
    """«покажи мои напоминания»: домен reminders РАЗРЕШЁН (read-кюс) → директива остаётся
    (не переусердствовали с подавлением)."""
    from sreda.runtime.react_preflight import route_domains
    _rr = route_domains("покажи мои напоминания")
    if not (_rr.directive and _rr.primary_domain == "reminders"):
        pytest.skip("премиса маршрутизатора изменилась")
    msgs = []
    install(deepseek=_Chat("ds"))
    _turn(_Chat("freddie", msgs_capture=msgs), thread="cf2", text="покажи мои напоминания")
    assert _rr.directive in _last_human_text(msgs[-1]), \
        "директива разрешённого домена (reminders) должна остаться"


# ─────────── #3: guard от петли domain_blocked + жёсткое сообщение ───────────
def test_loop_guard_stops_early_on_blocked(install):
    """Модель долбит заблокированный list_checklists на «как дела?» (web-only) → guard #3a
    обрывает после ≥2 domain_blocked, НЕ жжёт до _MAX_TURN_PASSES=8. И сообщение #3b — жёсткое."""
    msgs = []
    freddie = _Chat("freddie", msgs_capture=msgs,
                    responses=[_ai_call("list_checklists", "c1")])  # всегда одно и то же (заблокировано)
    install(deepseek=_Chat("ds"))
    _turn(freddie, thread="lg", text="как дела?")
    assert 0 < len(msgs) <= 4, f"петля должна оборваться рано (≤4), а не 8; проходов={len(msgs)}"
    # #3b: жёсткое сообщение блокировки дошло до модели (во входе следующего прохода)
    assert "НЕ зови" in _flat(msgs[-1]), "domain_blocked-сообщение должно быть жёстким на unified"
