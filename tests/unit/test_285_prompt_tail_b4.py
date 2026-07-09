"""#285 Фаза B срез B4: единый промпт (task-персона, не chat_fact) + честный хвост из bound (#279).

Пилляр 4 (анти-дрейф промпт↔bind): на execute-ходе единого пути
- системный промпт = task-персона (`_system_prompt`, `<persona>`), НЕ chat_fact;
- per-turn директива «доступны инструменты: …» из ФАКТИЧЕСКОГО bound + #279-семантика
  (способность есть; это про ТЕКУЩИЙ ход, не про умения; нужного нет → уточни, не отказывай);
- директива идёт ТОЛЬКО хвостом на user-роли (#247) — НЕ в системный промпт (кеш-префикс стабилен);
- пересобирается из bound (разный bound → разный хвост).

Юнит — на чистой `_unified_availability_directive`; интеграция — сквозь handle_turn (перехват
и bound-имён, и итоговых сообщений, что уходят в LLM).
"""

from __future__ import annotations

import asyncio
import types

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop

_TASK_TOOLS = ["list_reminders", "schedule_reminder", "add_task", "cancel_task",
               "need_family", "recall_memory"]
_WEB_TOOLS = ["web_search", "fetch_url", "get_weather"]


def _named(name):
    return types.SimpleNamespace(name=name)


def _ai_call(name, cid, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def _mk_tool(name, invoked):
    def _f(q: str = ""):
        invoked[name] = invoked.get(name, 0) + 1
        return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=name)


class _NoTrace:
    def __getattr__(self, _):
        return lambda *a, **k: None


class _Chat:
    """Фейк LLM: перехватывает и имена забинденных инструментов, и сообщения, ушедшие в invoke."""

    def __init__(self, label, *, bound_capture=None, msgs_capture=None, responses=None):
        self.label = label
        self._cap = bound_capture
        self._mcap = msgs_capture
        self._responses, self._i = list(responses or []), 0

    async def ainvoke(self, _msgs):
        return AIMessage(content="chat")

    def bind_tools(self, tools):
        if self._cap is not None:
            self._cap.setdefault(self.label, []).append(
                sorted(getattr(t, "name", "?") for t in tools))
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

    def _install(*, unified_flag=False, unified_tenants="", deepseek=None):
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1" if unified_flag else "0")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_TENANTS", unified_tenants)
        settings_mod.get_settings.cache_clear()
        inv = {}
        monkeypatch.setattr(react_loop, "build_slice_tools", lambda *a, **k: [
            _mk_tool(n, inv) for n in (_TASK_TOOLS + _WEB_TOOLS)])
        monkeypatch.setattr(react_loop, "_trace", _NoTrace())
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


def _sys_text(msgs):
    return "\n".join(str(getattr(m, "content", "")) for m in msgs if isinstance(m, SystemMessage))


def _last_human_text(msgs):
    hs = [m for m in msgs if isinstance(m, HumanMessage)]
    return str(hs[-1].content) if hs else ""


# ─────────── юнит: _unified_availability_directive ───────────
def test_availability_directive_lists_names_and_honesty():
    tools = [_named("add_task"), _named("web_search"), _named("recall_memory")]
    d = react_loop._unified_availability_directive(tools)
    assert "доступны инструменты:" in d
    for n in ("add_task", "recall_memory", "web_search"):
        assert n in d, d
    # порядок детерминирован (sorted) — стабильный текст хвоста
    assert d.index("add_task") < d.index("recall_memory") < d.index("web_search")
    # #285 канарейка-фикс тона: ВЕДЁТ с «ответь по существу, опираясь на результаты» (не со списка
    # тулов), honesty/условный write — фоном. #279-семантика сохранена.
    assert "ПО СУЩЕСТВУ" in d          # первичная директива — ответить по сути запроса
    assert "результаты" in d           # опираться на результаты инструментов (отчёт о действии)
    assert "текущий ход" in d
    assert "не умею" in d              # способность есть (honesty)
    assert "уточни" in d               # условный write-кларификатор


def test_availability_directive_empty_bound_still_honest():
    d = react_loop._unified_availability_directive([])
    assert "не умею" in d                    # честность есть даже без инструментов
    assert "ПО СУЩЕСТВУ" in d                # ведёт с ответа по сути
    assert "доступны инструменты:" not in d  # нечего перечислять


def test_availability_directive_reflects_bound_set():
    """Разный bound → разный хвост (анти-дрейф: хвост следует за фактическим bound каждый проход)."""
    d1 = react_loop._unified_availability_directive([_named("add_task"), _named("web_search")])
    d2 = react_loop._unified_availability_directive([_named("web_search")])
    assert "add_task" in d1 and "add_task" not in d2


# ─────────── интеграция: единый execute-ход ───────────
def test_unified_turn_uses_task_persona_and_userrole_tail(install):
    """execute-ход: системный промпт = task-персона (<persona>); честный хвост в ПОСЛЕДНЕМ
    user-сообщении; в системный промпт хвост НЕ дописан (OFF-ветка на unified запрещена)."""
    cap, msgs = {}, []
    freddie = _Chat("freddie", bound_capture=cap, msgs_capture=msgs)
    install(unified_flag=True, unified_tenants="t", deepseek=_Chat("ds"))
    _turn(freddie, thread="b4a", text="как дела?")
    assert msgs, "freddie должен был получить сообщения (task-путь единого)"
    last = msgs[-1]
    sys_txt = _sys_text(last)
    assert "<persona>" in sys_txt, "unified execute должен садиться на task-персону (_system_prompt)"
    # честный хвост — user-роль, НЕ система
    assert "доступны инструменты:" in _last_human_text(last), last
    assert "доступны инструменты:" not in sys_txt, "хвост НЕ должен попадать в системный промпт (кеш)"


def test_non_unified_turn_has_no_availability_tail(install):
    """Обычный тенант (флаг ON, но не в списке) → chat/fact web-only сплит; B4-хвост
    «доступны инструменты:» НЕ протекает в легаси-путь."""
    msgs = []
    ds = _Chat("ds", msgs_capture=msgs)
    freddie = _Chat("freddie")
    install(unified_flag=True, unified_tenants="canary", deepseek=ds)
    _turn(freddie, thread="b4b", text="как дела?", tenant="regular")
    joined = "\n".join(str(getattr(m, "content", "")) for lst in msgs for m in lst)
    assert "доступны инструменты:" not in joined, "B4-хвост не должен появляться на не-unified тенанте"


# ─────────── юнит: _effective_intent (Codex high+medium R1 MAJOR — одна персона во всех узлах) ───────────
def test_effective_intent_unified_forces_task():
    """На unified — ВСЕГДА "task" (даже если чекпойнт понесёт intent=chat/fact, даже без preflight):
    одна персона+политика согласованы во ВСЕХ узлах (chat/run_tools/route)."""
    assert react_loop._effective_intent({"unified_execute": True, "intent": "chat"}, True) == "task"
    assert react_loop._effective_intent({"unified_execute": True, "intent": "fact"}, False) == "task"
    assert react_loop._effective_intent({"unified_execute": True}, True) == "task"


def test_effective_intent_non_unified_preserves_197():
    """Не-unified — прежняя деривация #197: intent при preflight, иначе None (byte-identical OFF)."""
    assert react_loop._effective_intent({"intent": "chat"}, True) == "chat"
    assert react_loop._effective_intent({"intent": "task"}, True) == "task"
    assert react_loop._effective_intent({"intent": "chat"}, False) is None  # OFF → None
    assert react_loop._effective_intent({}, True) is None


# ─────────── интеграция: пересборка на КАЖДОМ проходе + resume ───────────
def test_unified_multipass_rebuilds_tail(install):
    """Хвост пересобирается на КАЖДОМ проходе: pass1 зовёт web_search → pass2 (после tool-результата)
    снова несёт task-персону + availability-хвост (анти-дрейф на многоходовке, не только 1-й проход)."""
    cap, msgs = {}, []
    freddie = _Chat("freddie", bound_capture=cap, msgs_capture=msgs,
                    responses=[_ai_call("web_search", "w1", query="погода"), AIMessage(content="Готово.")])
    install(unified_flag=True, unified_tenants="t", deepseek=_Chat("ds"))
    _turn(freddie, thread="b4mp", text="какая погода?")
    assert len(msgs) >= 2, f"ожидались 2+ прохода, было {len(msgs)}"
    last = msgs[-1]  # проход 2
    assert "<persona>" in _sys_text(last), "task-персона должна держаться и на 2-м проходе"
    assert "доступны инструменты:" in _last_human_text(last), "хвост должен пересобираться на 2-м проходе"
    # ДОКАЗАТЕЛЬСТВО пересборки из АКТУАЛЬНОГО bound (не статика/кеш pass1): хвост pass2 содержит РОВНО
    # директиву, построенную из имён bound pass2 (тот же bound-объект, что ушёл в bind_tools этого прохода).
    # Кеш из pass1 совпал бы только при равном bound; юнит test_availability_directive_reflects_bound_set
    # доказывает, что при РАЗНОМ bound директива различается → вместе = пересборка из живого bound.
    p2_names = cap.get("freddie", [[]])[-1]
    expected = react_loop._unified_availability_directive([_named(n) for n in p2_names])
    assert expected in _last_human_text(last), "хвост pass2 должен быть построен из bound ИМЕННО pass2"


def test_unified_resume_pass_keeps_persona_and_tail(install):
    """Resume-проход (после confirm-паузы) держит task-персону + availability-хвост — путь, где регрессия
    наиболее вероятна молча (если бы unified слетал с чекпойнта или возвращалась system-append ветка)."""
    cap, msgs = {}, []
    freddie = _Chat("freddie", bound_capture=cap, msgs_capture=msgs,
                    responses=[_ai_call("add_task", "c1", title="позвонить"), AIMessage(content="Готово.")])
    install(unified_flag=True, unified_tenants="t", deepseek=_Chat("ds"))
    r1 = _turn(freddie, thread="b4rs", text="расскажи как дела")  # unsignaled → candidate → пауза
    assert getattr(r1, "awaiting_confirm", False) is True, r1
    msgs.clear()  # интересует именно resume-проход
    _turn(freddie, thread="b4rs", text="да")  # resume
    assert msgs, "resume должен был вызвать chat заново"
    last = msgs[-1]
    assert "<persona>" in _sys_text(last), "resume: task-персона должна держаться"
    assert "доступны инструменты:" in _last_human_text(last), "resume: availability-хвост должен держаться"
