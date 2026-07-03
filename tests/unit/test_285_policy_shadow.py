"""#285 Фаза A (срез A1): TurnPolicy в тени — пин-тесты.

Чеклист приёмки #285 (issue, ядро №3 «миграция безопасна»): shadow byte-identical.
Здесь: (1) build_turn_policy выражает решения сплита корректно (чистые юниты);
(2) сайдкар в handle_turn: OFF → не исполняется вовсе; ON → зовётся с входами сплита;
(3) byte-identical: bound-наборы и вызовы инструментов идентичны при флаге ON и OFF
    (shadow не управляет исполнением и не мутирует legacy-каналы — иначе bound бы разошёлся).
Хранение полиси в трейсе (наблюдаемый исход) — тест среза A2 (persist-wiring).
Харнес скопирован минимально из test_react_preflight_197 (fixtures файло-локальны).
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop, react_policy
from sreda.runtime.react_policy import WEB_ONLY_TOOL_NAMES, build_turn_policy

_TASK_TOOLS = ["list_reminders", "schedule_reminder", "add_task", "cancel_task",
               "need_family", "recall_memory"]
_WEB_TOOLS = ["web_search", "fetch_url", "get_weather"]


def _mk_tool(name, invoked):
    def _f(q: str = ""):
        invoked[name] = invoked.get(name, 0) + 1
        return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=name)


def _toolset(invoked):
    return [_mk_tool(n, invoked) for n in (_TASK_TOOLS + _WEB_TOOLS)]


class _NoTrace:
    def __getattr__(self, _):
        return lambda *a, **k: None


class _Chat:
    def __init__(self, label, classify="chat", responses=None, bound_capture=None, calls=None):
        self.label, self._classify = label, classify
        self._responses = list(responses or [])
        self._i, self._cap = 0, bound_capture
        self._calls = calls if calls is not None else {}

    async def ainvoke(self, _msgs):
        self._calls["classify_" + self.label] = self._calls.get("classify_" + self.label, 0) + 1
        return AIMessage(content=self._classify)

    def bind_tools(self, tools):
        if self._cap is not None:
            self._cap.setdefault(self.label, []).append(sorted(getattr(t, "name", "?") for t in tools))
        outer = self

        def _inv(_msgs):
            outer._calls["invoke_" + outer.label] = outer._calls.get("invoke_" + outer.label, 0) + 1
            r = (outer._responses[min(outer._i, len(outer._responses) - 1)]
                 if outer._responses else AIMessage(content="resp-" + outer.label))
            outer._i += 1
            return r
        return RunnableLambda(_inv)


@pytest.fixture
def install(monkeypatch):
    from sreda.config import settings as settings_mod

    def _install(*, preflight=True, unified=False, deepseek=None, invoked=None):
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1" if preflight else "0")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1" if unified else "0")
        settings_mod.get_settings.cache_clear()
        inv = invoked if invoked is not None else {}
        monkeypatch.setattr(react_loop, "build_slice_tools", lambda *a, **k: _toolset(inv))
        monkeypatch.setattr(react_loop, "_trace", _NoTrace())
        monkeypatch.setattr(react_loop, "_persist_debug_turn", lambda **k: None)
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


# ───────────── чистые юниты build_turn_policy ─────────────

def _bp(intent, ar=None, aw=None, caps=None):
    return build_turn_policy(intent=intent, router_allowed_read=ar, router_allowed_write=aw,
                             chat_timeout_sec=15.0, task_timeout_sec=60.0,
                             chat_provider="openrouter-deepseek", search_caps=caps)


def test_policy_chat():
    p = _bp("chat", caps={"web_search": 1, "fetch_url": 2})
    assert p["prompt_variant"] == "chat_fact" and p["web_scope_only"] is True
    assert p["allowed_write_domains"] == [] and p["allowed_read_domains"] is None
    assert p["guard_scope"] == "off"
    assert p["provider_profile"]["timeout_sec"] == 15.0
    assert p["provider_profile"]["provider_hint"] == "openrouter-deepseek"
    assert p["search_budget"] == {"web_search": 1, "fetch_url": 2}


def test_policy_task_with_router_channels():
    p = _bp("task", ar=["tasks", "checklists"], aw=["tasks"])
    assert p["prompt_variant"] == "task" and p["web_scope_only"] is False
    assert p["allowed_read_domains"] == ["tasks", "checklists"]
    assert p["allowed_write_domains"] == ["tasks"]
    assert p["guard_scope"] == "legacy"
    assert p["provider_profile"]["timeout_sec"] == 60.0
    assert p["search_budget"] is None


def test_policy_off_intent_none():
    """preflight OFF (intent=None) → легаси task-семантика, фильтры None (без ограничений)."""
    p = _bp(None)
    assert p["prompt_variant"] == "task" and p["web_scope_only"] is False
    assert p["allowed_read_domains"] is None and p["allowed_write_domains"] is None


def test_web_only_pin_matches_preflight():
    """Пин синхронности WEB_ONLY_TOOL_NAMES с #197 (react_policy не импортирует preflight)."""
    from sreda.runtime.react_preflight import _WEB_ONLY_TOOL_NAMES
    assert set(WEB_ONLY_TOOL_NAMES) == set(_WEB_ONLY_TOOL_NAMES)


# ───────────── сайдкар в handle_turn ─────────────

def test_flag_off_sidecar_not_executed(install, monkeypatch):
    """OFF (дефолт): полиси-код на пути НЕ исполняется вовсе (zero-overhead откат)."""
    calls = {}
    seen = []
    monkeypatch.setattr(react_policy, "build_turn_policy",
                        lambda **kw: seen.append(kw) or {"v": 1})
    freddie = _Chat("freddie", classify="chat", calls=calls)
    install(preflight=True, unified=False, deepseek=_Chat("deepseek", calls=calls))
    _turn(freddie, thread="off-1", text="как настроение?")
    assert seen == []


def test_flag_on_sidecar_chatfact_inputs(install, monkeypatch):
    """ON + chat-интент: сайдкар зовётся с входами сплита (intent, капы chat, без каналов)."""
    seen = []
    real = react_policy.build_turn_policy
    monkeypatch.setattr(react_policy, "build_turn_policy",
                        lambda **kw: seen.append(kw) or real(**kw))
    calls = {}
    freddie = _Chat("freddie", classify="chat", calls=calls)
    install(preflight=True, unified=True, deepseek=_Chat("deepseek", calls=calls))
    _turn(freddie, thread="on-1", text="как настроение?")
    assert len(seen) == 1
    kw = seen[0]
    assert kw["intent"] == "chat"
    assert kw["search_caps"] == {"web_search": 1, "fetch_url": 2}
    assert kw["router_allowed_read"] is None and kw["router_allowed_write"] is None
    assert kw["chat_timeout_sec"] == 15.0 and kw["task_timeout_sec"] == 60.0


def test_flag_on_sidecar_task_inputs(install, monkeypatch):
    """ON + task-интент (классификатор): сайдкар получает task без капов."""
    seen = []
    real = react_policy.build_turn_policy
    monkeypatch.setattr(react_policy, "build_turn_policy",
                        lambda **kw: seen.append(kw) or real(**kw))
    calls = {}
    freddie = _Chat("freddie", classify="task", calls=calls)
    install(preflight=True, unified=True, deepseek=_Chat("deepseek", calls=calls))
    _turn(freddie, thread="on-2", text="разбери мою неделю")
    assert len(seen) == 1 and seen[0]["intent"] == "task" and seen[0]["search_caps"] is None


def test_flag_on_sidecar_failure_does_not_break_turn(install, monkeypatch):
    """Сбой сайдкара НЕ роняет ход (try/except, полиси → None)."""
    monkeypatch.setattr(react_policy, "build_turn_policy",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom-shadow")))
    calls = {}
    freddie = _Chat("freddie", classify="chat", calls=calls)
    install(preflight=True, unified=True, deepseek=_Chat("deepseek", calls=calls))
    reply = _turn(freddie, thread="on-3", text="как настроение?")
    # НОРМАЛЬНЫЙ ответ стаба, НЕ safe-reply внешнего catch-all (иначе тест вакуумен: handle_turn
    # никогда не возвращает None — R1 MAJOR субагента, класс g-055).
    assert "resp-deepseek" in str(reply)


# ───────────── byte-identical: ON == OFF ─────────────

def test_flag_on_byte_identical_chat_and_task(install):
    """Ядро №3 чеклиста: shadow НЕ меняет исполнение — bound-наборы и вызовы инструментов
    идентичны при unified ON и OFF (для chat/fact И task ходов). Заодно доказывает, что
    сайдкар не мутирует legacy-каналы: их запись сузила бы bound (фильтр доменов)."""
    results = {}
    for flag in (False, True):
        for intent, text in (("chat", "как настроение?"), ("task", "разбери мою неделю")):
            cap, calls, inv = {}, {}, {}
            freddie = _Chat("freddie", classify=intent, bound_capture=cap, calls=calls)
            ds = _Chat("deepseek", bound_capture=cap, calls=calls)
            install(preflight=True, unified=flag, deepseek=ds, invoked=inv)
            _turn(freddie, thread=f"bi-{flag}-{intent}", text=text)
            results[(flag, intent)] = (cap, calls, inv)
    for intent in ("chat", "task"):
        off_cap, off_calls, off_inv = results[(False, intent)]
        on_cap, on_calls, on_inv = results[(True, intent)]
        assert on_cap == off_cap, f"bound разошёлся на {intent}"
        assert on_calls == off_calls, f"вызовы LLM разошлись на {intent}"
        assert on_inv == off_inv, f"вызовы инструментов разошлись на {intent}"
