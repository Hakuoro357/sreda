"""#197 — тесты preflight intent-router. Имена тестов СИНХРОНЫ с чеклистом приёмки (ПРАВИЛО #7).

Покрытие:
- юнит: _must_task (high-precision +/-), _parse_intent (строгий), classify_intent (fail-open/prev),
  _bind_for (web-only/byte-identical), _count_executed_tool, _WEB_ONLY_TOOL_NAMES sync, scoped-промпт;
- graph (fake LLM + fake tools, БЕЗ сети/БД): web-only bind+dispatch, need_family-галлюцинация→недоступен,
  cap web_search/fetch_url≤1 (chat/fact) и НЕ-cap (task), state-driven выбор модели, guard-no-recovery,
  fallback chat→Фредди web-only (НЕ task), misconfig→Фредди web-only, OFF byte-identical + игнор stored intent,
  resume сохраняет task-scope (классификатор не зван), атрибуция расхода deepseek, get_chat_llm(provider=...),
  поля трейса.
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool
from langgraph.types import interrupt

from sreda.runtime import react_loop
from sreda.runtime.react_preflight import (
    _HINT_CHECKLIST, _HINT_REMINDER, _HINT_TASK, _MUST_TASK_PATTERNS,
    _WEB_ONLY_TOOL_NAMES, _must_task, _parse_intent, _section_hint,
    chat_fact_system_prompt, classify_intent,
)
from sreda.runtime.react_loop import _bind_for, _count_executed_tool, _select_tools


# ───────────────────────────── харнес ─────────────────────────────
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


def _toolset(invoked, interrupt_names=()):
    return [_mk_tool(n, invoked, interrupt_tool=(n in interrupt_names))
            for n in (_TASK_TOOLS + _WEB_TOOLS)]


def _ai_tools(*specs):
    """AIMessage с tool_calls: (name, id)."""
    return AIMessage(content="", tool_calls=[{"name": n, "args": {}, "id": i} for n, i in specs])


class _Chat:
    """Фейковый LLM. ainvoke → классификатор (одно слово). bind_tools().invoke → последовательность
    ответов; пишет состав bound в bound_capture; считает вызовы в calls; raise_on_invoke → сбой invoke."""

    def __init__(self, label, *, classify="task", responses=None, bound_capture=None,
                 calls=None, raise_on_invoke=False, sp_capture=None, msgs_capture=None,
                 types_capture=None):
        self.label = label
        self._classify = classify
        self._responses = list(responses or [])
        self._i = 0
        self._cap = bound_capture
        self._calls = calls if calls is not None else {}
        self._raise = raise_on_invoke
        self._spcap = sp_capture  # #215: захват system-промпта, что увидела модель
        self._msgcap = msgs_capture  # #247: захват ВСЕХ сообщений (content) каждого вызова
        self._typecap = types_capture  # #247 R2: захват РОЛЕЙ (типов сообщений) — защита от регресса роли хвоста

    async def ainvoke(self, _msgs):
        self._calls["classify_" + self.label] = self._calls.get("classify_" + self.label, 0) + 1
        return AIMessage(content=self._classify)

    def bind_tools(self, tools):
        if self._cap is not None:
            self._cap.setdefault(self.label, []).append(sorted(getattr(t, "name", "?") for t in tools))
        outer = self

        def _inv(_msgs):
            outer._calls["invoke_" + outer.label] = outer._calls.get("invoke_" + outer.label, 0) + 1
            if outer._spcap is not None and _msgs:
                outer._spcap.append(getattr(_msgs[0], "content", ""))
            if outer._msgcap is not None:
                outer._msgcap.append([getattr(m, "content", "") for m in (_msgs or [])])
            if outer._typecap is not None:
                outer._typecap.append([type(m).__name__ for m in (_msgs or [])])
            if outer._raise:
                raise RuntimeError("boom-" + outer.label)
            if outer._responses:
                r = outer._responses[min(outer._i, len(outer._responses) - 1)]
            else:
                r = AIMessage(content="resp-" + outer.label)
            outer._i += 1
            return r
        return RunnableLambda(_inv)


class _NoTrace:
    def __getattr__(self, _):
        return lambda *a, **k: None


@pytest.fixture
def install(monkeypatch):
    """Установить окружение preflight + замокать инструменты/трейс/usage (без сети/БД)."""
    from sreda.config import settings as settings_mod

    def _install(*, on=True, deepseek=None, provider="openrouter-deepseek",
                 invoked=None, interrupt_names=(), capture_get_chat_llm=None):
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1" if on else "0")
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_CHAT_PROVIDER", provider)
        try:
            settings_mod.get_settings.cache_clear()
        except Exception:
            pass
        inv = invoked if invoked is not None else {}
        monkeypatch.setattr(react_loop, "build_slice_tools",
                            lambda *a, **k: _toolset(inv, interrupt_names))
        monkeypatch.setattr(react_loop, "_trace", _NoTrace())
        monkeypatch.setattr(react_loop, "_persist_debug_turn", lambda **k: None)
        monkeypatch.setattr(react_loop, "_record_react_usage", lambda **k: None)
        import sreda.services.llm as llm_mod

        def _gc(*a, **k):
            if capture_get_chat_llm is not None:
                capture_get_chat_llm["args"] = a
                capture_get_chat_llm["kwargs"] = dict(k)
            return deepseek
        monkeypatch.setattr(llm_mod, "get_chat_llm", _gc)
        return inv
    yield _install
    try:
        settings_mod.get_settings.cache_clear()
    except Exception:
        pass


def _turn(freddie, *, thread, text, resume_only=False, expected=""):
    return asyncio.run(react_loop.handle_turn(
        session=None, tenant_id="t", user_id="u", thread_id=thread,
        llm=freddie, user_text=text, inbound_message_id=f"{thread}:{text[:12]}",
        channel="react", resume_only=resume_only, expected_confirm_id=expected,
        provider_key="inception-mercury2", fallback_llm=None))


# ───────────────────────── юнит: _must_task ─────────────────────────
def test_must_task_high_precision():
    for t in ["напомни купить молоко", "перенеси на завтра", "покажи мои задачи",
              "добавь в список хлеб", "запомни что я люблю кофе", "что у меня сегодня"]:
        assert _must_task(t) is True, t
    for t in ["расскажи мне про Пушкина", "найди мне рецепт борща", "кто такой Гагарин",
              "как дела", "", "сколько планет в солнечной системе"]:
        assert _must_task(t) is False, t
    # эллипсис убран (code-review R1): короткий follow-up после task НЕ форсится в task
    assert _must_task("да", prev_intent="task") is False
    assert _must_task("кто Пушкин?", prev_intent="task") is False


def test_must_task_patterns_nonempty():
    assert len(_MUST_TASK_PATTERNS) >= 10


# ───────────────────────── юнит: classify parse ─────────────────────────
def test_classify_parses_strict():
    assert _parse_intent("task") == "task"
    assert _parse_intent("  CHAT  ") == "chat"
    assert _parse_intent("fact.") == "fact"
    assert _parse_intent("бла-бла") == "task"
    assert _parse_intent("") == "task"
    assert _parse_intent(None) == "task"
    assert _parse_intent("I think chat") == "task"


def test_classifier_failure_fail_open():
    class _Boom:
        async def ainvoke(self, _m):
            raise RuntimeError("net")
    out = asyncio.run(classify_intent([], "что угодно", None, _Boom(), timeout=1.0))
    assert out == "task"


def test_prev_intent_carried():
    class _F:
        async def ainvoke(self, _m):
            return AIMessage(content="chat")
    out = asyncio.run(classify_intent([HumanMessage("анекдот")], "ещё", "chat", _F()))
    assert out == "chat"


def test_intent_switch_not_sticky():
    assert _must_task("напомни купить хлеб", prev_intent="chat") is True


def test_section_hint_maps_words_to_section():
    # #215: «дела»/«списки» → чек-листы; «задачи»/«расписание» → tasks; «напоминания» → reminders
    assert _section_hint("покажи дела") == _HINT_CHECKLIST
    assert _section_hint("мои списки") == _HINT_CHECKLIST
    assert _section_hint("список кино") == _HINT_CHECKLIST
    assert _section_hint("покажи задачи") == _HINT_TASK
    assert _section_hint("что у меня в расписании") == _HINT_TASK
    assert _section_hint("напомни купить молоко") == _HINT_REMINDER
    assert _section_hint("мои напоминания") == _HINT_REMINDER
    # нет ложного матча на «сделай»/«делать» (подстрока «дела»)
    assert _section_hint("сделай это") is None
    assert _section_hint("надо что-то делать") is None
    assert _section_hint("как настроение") is None
    assert _section_hint("") is None
    # приоритет: напоминания > задачи > списки
    assert _section_hint("напомни про задачи") == _HINT_REMINDER


def test_section_hint_reaches_model_on_task(install):
    # «покажи дела» → must_task → task → Фредди; директива «используй list_checklists» в промпте модели
    spcap = []
    freddie = _Chat("freddie", classify="task", sp_capture=spcap,
                    responses=[AIMessage(content="вот твои списки")])
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    _turn(freddie, thread="sec-hint", text="покажи дела")
    assert spcap, "модель не получила промпт"
    assert "list_checklists" in spcap[-1]
    assert "list_reminders" in spcap[-1]  # директива явно говорит НЕ показывать напоминания


def test_off_no_section_hint(install):
    # #215 (code-review R1, все 3 ревьюера MAJOR): на OFF (eff=None) секц-подсказка НЕ добавляется —
    # сохраняем byte-identical OFF (инвариант T3 #197), даже на слове-разделе «покажи дела».
    spcap = []
    freddie = _Chat("freddie", classify="task", sp_capture=spcap,
                    responses=[AIMessage(content="ок")])
    install(on=False, deepseek=_Chat("deepseek"), invoked={})
    _turn(freddie, thread="off-nosec", text="покажи дела")
    assert spcap, "модель не получила промпт"
    assert _HINT_CHECKLIST not in spcap[-1]
    assert "list_checklists" not in spcap[-1]


# ───────────────────────── юнит: _bind_for ─────────────────────────
def test_byte_identical_bind_for_none():
    inv = {}
    tools = _toolset(inv)
    for intent in (None, "task"):
        got = [t.name for t in _bind_for(tools, ["web", "shopping"], intent)]
        exp = [t.name for t in _select_tools(tools, ["web", "shopping"])]
        assert got == exp, intent


def test_bind_for_chat_web_only_and_get_weather():
    inv = {}
    tools = _toolset(inv)
    for intent in ("chat", "fact"):
        names = {t.name for t in _bind_for(tools, ["web", "shopping"], intent)}
        assert names == set(_WEB_ONLY_TOOL_NAMES), intent
        assert "get_weather" in names
        assert "list_reminders" not in names and "need_family" not in names


def test_web_search_tier_gating_web_only_names_sync():
    from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
    web_family = {n for n, f in TOOL_FAMILY_MANIFEST.items() if f == "web"}
    assert set(_WEB_ONLY_TOOL_NAMES) == web_family


def test_count_executed_tool_ignores_search_limit():
    msgs = [
        HumanMessage("вопрос"),
        ToolMessage(content="ok", name="web_search", tool_call_id="a",
                    artifact={"result_kind": "ok"}),
        ToolMessage(content="лимит", name="web_search", tool_call_id="b",
                    artifact={"result_kind": "search_limit"}),
    ]
    assert _count_executed_tool(msgs, "web_search") == 1


def test_chat_fact_prompt_no_productivity_tool_names():
    sp = chat_fact_system_prompt("2026-06-24")
    for tool_name in ("schedule_reminder", "add_task", "list_reminders", "need_family",
                      "recall_memory", "cancel_task"):
        assert tool_name not in sp, tool_name
    assert "web_search" in sp


# ───────────────────────── graph: scope web-only ─────────────────────────
def test_chat_tools_web_only_bind_and_dispatch(install):
    inv = {}
    cap = {}
    deepseek = _Chat("deepseek", bound_capture=cap,
                     responses=[_ai_tools(("need_family", "n1"), ("list_reminders", "l1")),
                                AIMessage(content="готово")])
    freddie = _Chat("freddie", classify="chat", bound_capture=cap)
    install(on=True, deepseek=deepseek, invoked=inv)
    reply = _turn(freddie, thread="web-only", text="расскажи анекдот")
    assert cap["deepseek"][0] == sorted(_WEB_ONLY_TOOL_NAMES)
    assert inv.get("need_family", 0) == 0
    assert inv.get("list_reminders", 0) == 0
    assert "готово" in reply


def test_state_driven_model_select(install):
    cap = {}
    # #216: маркер ответа НЕ "deepseek" — гард личности (_redact_identity) редактирует
    # имена провайдеров/моделей в тексте ответа. Используем нейтральный sentinel,
    # тест по-прежнему проверяет, что ответ пришёл по deepseek-пути (модель в cap ниже).
    deepseek = _Chat("deepseek", bound_capture=cap, responses=[AIMessage(content="ответ-маркер-ДС")])
    freddie = _Chat("freddie", classify="chat", bound_capture=cap, calls={})
    install(on=True, deepseek=deepseek, invoked={})
    r1 = _turn(freddie, thread="sds-chat", text="как настроение")
    assert "маркер-ДС" in r1
    assert cap["deepseek"][-1] == sorted(_WEB_ONLY_TOOL_NAMES)

    cap2 = {}
    deepseek2 = _Chat("deepseek", bound_capture=cap2)
    freddie2 = _Chat("freddie", classify="task", bound_capture=cap2,
                     responses=[AIMessage(content="ответ-freddie")])
    install(on=True, deepseek=deepseek2, invoked={})
    r2 = _turn(freddie2, thread="sds-task", text="что там по делам")
    assert "freddie" in r2
    assert "list_reminders" in cap2["freddie"][-1]
    assert "deepseek" not in cap2


# ───────────────────────── graph: лимит поиска ─────────────────────────
def test_web_search_batch_capped_one(install):
    inv = {}
    deepseek = _Chat("deepseek",
                     responses=[_ai_tools(("web_search", "w1"), ("web_search", "w2")),
                                AIMessage(content="итог")])
    freddie = _Chat("freddie", classify="chat")
    install(on=True, deepseek=deepseek, invoked=inv)
    reply = _turn(freddie, thread="cap-ws", text="что нового в мире")
    assert inv.get("web_search", 0) == 1
    assert "итог" in reply


def test_fetch_url_capped_chat_two(install):
    # #215: chat fetch_url ≤2 → из 3 исполнятся 2, 3-й → synthetic limit
    inv = {}
    deepseek = _Chat("deepseek",
                     responses=[_ai_tools(("fetch_url", "f1"), ("fetch_url", "f2"), ("fetch_url", "f3")),
                                AIMessage(content="готово")])
    freddie = _Chat("freddie", classify="chat")
    install(on=True, deepseek=deepseek, invoked=inv)
    _turn(freddie, thread="cap-fu", text="открой пару ссылок")
    assert inv.get("fetch_url", 0) == 2


def test_fact_allows_more_searches(install):
    # #215: fact web_search ≤3 (смягчён с ≤1 — иначе факты упираются в лимит) → из 4 исполнятся 3
    inv = {}
    deepseek = _Chat("deepseek",
                     responses=[_ai_tools(("web_search", "w1"), ("web_search", "w2"),
                                          ("web_search", "w3"), ("web_search", "w4")),
                                AIMessage(content="ответ по факту")])
    freddie = _Chat("freddie", classify="fact")
    install(on=True, deepseek=deepseek, invoked=inv)
    _turn(freddie, thread="cap-fact", text="кто выиграл финал лиги чемпионов")
    assert inv.get("web_search", 0) == 3


def test_task_allows_multiple_searches(install):
    inv = {}
    freddie = _Chat("freddie", classify="task",
                    responses=[_ai_tools(("web_search", "w1"), ("web_search", "w2")),
                               AIMessage(content="done")])
    install(on=True, deepseek=_Chat("deepseek"), invoked=inv)
    _turn(freddie, thread="task-multi", text="посмотри новости по делам")
    assert inv.get("web_search", 0) == 2


# ───────────────────────── graph: guard / OFF ─────────────────────────
def test_guard_no_recovery_for_chat(install):
    deepseek = _Chat("deepseek", responses=[AIMessage(content="к сожалению, не получится помочь")])
    freddie = _Chat("freddie", classify="chat", calls={})
    install(on=True, deepseek=deepseek, invoked={})
    reply = _turn(freddie, thread="guard-chat", text="сыграем в игру")
    assert "не получится помочь" in reply
    assert "разумное число шагов" not in reply


def test_flag_off_byte_identical(install):
    cap = {}
    freddie = _Chat("freddie", classify="chat", bound_capture=cap, calls={},
                    responses=[AIMessage(content="ответ-off")])
    install(on=False, deepseek=_Chat("deepseek"), invoked={})
    reply = _turn(freddie, thread="off-bi", text="что угодно")
    assert "ответ-off" in reply
    assert freddie._calls.get("classify_freddie", 0) == 0
    assert "list_reminders" in cap["freddie"][-1]


def test_off_ignores_stored_intent(install):
    deepseek = _Chat("deepseek", responses=[AIMessage(content="ха-ха")])
    freddie1 = _Chat("freddie", classify="chat")
    install(on=True, deepseek=deepseek, invoked={})
    _turn(freddie1, thread="off-stored", text="расскажи анекдот")
    cap = {}
    freddie2 = _Chat("freddie", classify="chat", bound_capture=cap, calls={},
                     responses=[AIMessage(content="ок")])
    install(on=False, deepseek=_Chat("deepseek"), invoked={})
    _turn(freddie2, thread="off-stored", text="что по плану")
    assert freddie2._calls.get("classify_freddie", 0) == 0
    assert "list_reminders" in cap["freddie"][-1]


# ───────────────────────── graph: fallback / misconfig ─────────────────────────
def test_fallback_chat_web_only_honest(install):
    cap = {}
    deepseek = _Chat("deepseek", bound_capture=cap, raise_on_invoke=True)
    freddie = _Chat("freddie", classify="chat", bound_capture=cap,
                    responses=[AIMessage(content="честный ответ")])
    install(on=True, deepseek=deepseek, invoked={})
    reply = _turn(freddie, thread="fb-chat", text="как считаешь")
    assert "честный ответ" in reply
    assert cap["freddie"][-1] == sorted(_WEB_ONLY_TOOL_NAMES)


def test_fallback_double_fail_safe_no_full_tools(install):
    cap = {}
    deepseek = _Chat("deepseek", bound_capture=cap, raise_on_invoke=True)
    freddie = _Chat("freddie", classify="chat", bound_capture=cap, raise_on_invoke=True)
    install(on=True, deepseek=deepseek, invoked={})
    reply = _turn(freddie, thread="fb-double", text="что думаешь")
    assert str(reply)
    for b in cap.get("freddie", []) + cap.get("deepseek", []):
        assert b == sorted(_WEB_ONLY_TOOL_NAMES)


def test_provider_misconfig_fail_open(install):
    cap = {}
    freddie = _Chat("freddie", classify="chat", bound_capture=cap,
                    responses=[AIMessage(content="ответ")])
    install(on=True, deepseek=None, invoked={})
    reply = _turn(freddie, thread="misconfig", text="поболтаем")
    assert "ответ" in reply
    assert cap["freddie"][-1] == sorted(_WEB_ONLY_TOOL_NAMES)


# ───────────────────────── graph: usage / get_chat_llm / трейс ─────────────────────────
def test_usage_attributed_to_deepseek(install, monkeypatch):
    sink = {}
    deepseek = _Chat("deepseek", responses=[AIMessage(content="ответ")])
    freddie = _Chat("freddie", classify="chat")
    install(on=True, deepseek=deepseek, invoked={})
    monkeypatch.setattr(react_loop, "_record_react_usage",
                        lambda **k: sink.setdefault("pk", k.get("provider_key")))
    _turn(freddie, thread="usage", text="как погода настроение")
    assert sink.get("pk") == "openrouter-deepseek"


def test_get_chat_llm_keyword_and_flag(install):
    cap_gc = {}
    deepseek = _Chat("deepseek", responses=[AIMessage(content="ответ")])
    freddie = _Chat("freddie", classify="chat")
    install(on=True, deepseek=deepseek, provider="openrouter-deepseek",
            invoked={}, capture_get_chat_llm=cap_gc)
    _turn(freddie, thread="getllm", text="привет как ты")
    assert cap_gc["kwargs"].get("provider") == "openrouter-deepseek"
    assert cap_gc["args"] == ()


def test_preflight_trace_fields(install):
    inv = {}
    install(on=True, deepseek=None, invoked=inv)
    deepseek = _Chat("deepseek", responses=[AIMessage(content="ответ")])
    freddie = _Chat("freddie", classify="chat")
    g = react_loop._build_graph(
        freddie, _toolset(inv), tenant_id="t", user_id="u", today_str="2026-06-24",
        session=None, provider_key="inception-mercury2",
        deepseek_llm=deepseek, chat_prompt="cp", deepseek_provider_key="openrouter-deepseek",
        preflight_enabled=True)
    res = g.invoke(
        {"messages": [HumanMessage("привет")], "turn_key": "tk", "active_families": [],
         "guard_attempted_families": [], "turn_pass_count": 0, "guard_nudge": "",
         "wrote_unkeyed": False, "intent": "chat",
         "intent_meta": {"source": "classifier", "must_task": False, "classifier_raw": "chat"}},
        {"configurable": {"thread_id": "trace-fields"}})
    lc = res["llm_calls"][0]
    assert lc["intent"] == "chat"
    assert lc["tool_scope"] == "web"
    assert lc["selected_provider"] == "openrouter-deepseek"
    assert "web_search_count" in lc
    assert lc["intent_source"] == "classifier"
    assert lc["must_task"] is False
    assert lc["classifier_raw"] == "chat"


# ───────────────────────── graph: resume ─────────────────────────
def test_resume_keeps_task_scope(install):
    inv = {}
    freddie = _Chat("freddie", classify="task", calls={},
                    responses=[_ai_tools(("schedule_reminder", "s1")),
                               AIMessage(content="готово")])
    install(on=True, deepseek=_Chat("deepseek"), invoked=inv,
            interrupt_names=("schedule_reminder",))
    # текст БЕЗ must_task-паттерна → классификатор срабатывает на свежем ходе (classify="task")
    r1 = _turn(freddie, thread="resume", text="хочу кое-что запланировать на вечер")
    assert getattr(r1, "awaiting_confirm", False) is True
    assert freddie._calls.get("classify_freddie", 0) == 1   # свежий ход классифицирован
    r2 = _turn(freddie, thread="resume", text="да")
    assert "готово" in r2
    assert freddie._calls.get("classify_freddie", 0) == 1   # resume НЕ классифицируется повторно


# ───────────────────────── #221 Ф3: проводка доменного скоупа в граф ─────────────────────────
def test_domain_scope_mode_normalization(install, monkeypatch):
    """react_domain_scope нормализует значение флага; мусор/пусто → disabled (fail-safe)."""
    from sreda.config import settings as sm
    for raw, exp in [("execute", "execute"), ("SHADOW", "shadow"), ("  disabled ", "disabled"),
                     ("bogus", "disabled"), ("", "disabled")]:
        monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", raw)
        sm.get_settings.cache_clear()
        assert sm.get_settings().react_domain_scope == exp


def test_domain_scope_disabled_byte_identical(install, monkeypatch):
    """disabled (дефолт) + pruned тенант: набор НЕ фильтруется доменом — add_task/recall_memory на месте."""
    from sreda.config import settings as sm
    cap = {}
    freddie = _Chat("freddie", classify="task", bound_capture=cap, responses=[AIMessage(content="ок")])
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "disabled")
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    sm.get_settings.cache_clear()
    _turn(freddie, thread="dsm-off", text="напомни купить молоко")
    bound = cap["freddie"][-1]
    assert "add_task" in bound and "recall_memory" in bound  # без доменного фильтра (byte-identical)


def test_domain_scope_execute_scopes_tools(install, monkeypatch):
    """execute + pruned: на reminders-маршруте фильтр срезает чужие домены (add_task=write tasks, recall=read memory),
    оставляя reminders-инструменты и мета (need_family). Доменный скоуп работает в живом графе."""
    from sreda.config import settings as sm
    cap = {}
    freddie = _Chat("freddie", classify="task", bound_capture=cap, responses=[AIMessage(content="ок")])
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "t")  # Ф4: тенант в канареечном списке
    sm.get_settings.cache_clear()
    _turn(freddie, thread="dsm-exec", text="напомни купить молоко")
    bound = cap["freddie"][-1]
    assert "schedule_reminder" in bound and "list_reminders" in bound  # reminders-домен разрешён
    assert "need_family" in bound                                      # мета всегда
    assert "add_task" not in bound                                     # write tasks ⊄ allowed_write={reminders}
    assert "recall_memory" not in bound                               # read memory ⊄ allowed_read={reminders}


def test_domain_scope_no_stale_leak_across_turns(install, monkeypatch):
    """R1 CRITICAL: после execute-хода следующий disabled-ход в ТОМ ЖЕ треде НЕ наследует фильтр (router_allowed
    сброшен в None на свежем ходу; last-value канал иначе переживал бы invoke)."""
    from sreda.config import settings as sm
    cap = {}
    freddie = _Chat("freddie", classify="task", bound_capture=cap,
                    responses=[AIMessage(content="ок"), AIMessage(content="ок")])
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "t")  # Ф4: канареечный список
    sm.get_settings.cache_clear()
    _turn(freddie, thread="stale", text="напомни купить молоко")  # execute → router_allowed={reminders} в чекпойнте
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "disabled")
    sm.get_settings.cache_clear()
    _turn(freddie, thread="stale", text="покажи задачи")          # тот же тред, disabled → byte-identical
    bound = cap["freddie"][-1]
    assert "add_task" in bound and "recall_memory" in bound       # stale-фильтр НЕ унаследован


def test_domain_scope_shadow_is_legacy(install, monkeypatch):
    """shadow: исполнение legacy (фильтр НЕ применяется), ход не падает — add_task на месте."""
    from sreda.config import settings as sm
    cap = {}
    freddie = _Chat("freddie", classify="task", bound_capture=cap, responses=[AIMessage(content="ок")])
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "shadow")
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    sm.get_settings.cache_clear()
    r = _turn(freddie, thread="shadow", text="напомни купить молоко")
    assert "ок" in r
    assert "add_task" in cap["freddie"][-1]  # shadow = legacy, доменного фильтра нет


def test_domain_scope_execute_classify_failure_failopen(install, monkeypatch):
    """execute + нет детерм. домена: сбой classify_domains → legacy fail-open (фильтр не применён, ход цел)."""
    from sreda.config import settings as sm
    from sreda.runtime import react_preflight as rp
    cap = {}
    freddie = _Chat("freddie", classify="task", bound_capture=cap, responses=[AIMessage(content="ок")])
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "t")  # Ф4: канареечный список
    sm.get_settings.cache_clear()

    async def _boom(*a, **k):
        raise RuntimeError("classify boom")
    monkeypatch.setattr(rp, "classify_domains", _boom)
    r = _turn(freddie, thread="exec-fail", text="расскажи что-нибудь интересное")  # нет домена → classify → boom
    assert "ок" in r                          # ход не упал (try/except → legacy)
    assert "add_task" in cap["freddie"][-1]   # legacy fail-open: доменный фильтр НЕ применён


def test_domain_scope_execute_guard_no_domain_widening_267_a4(install, monkeypatch):
    """#267 A4 (Борис «роутер побеждает», ЗАМЕНЯЕТ #202 read-recovery): в execute guard НЕ расширяет
    домены роутера на отказе. Мис-классиф. (роутер=task, юзер про погоду) → recall_memory НЕ появляется
    на 2-м проходе (раздел НЕ открыт); write-гейт по-прежнему цел. Планировщик остаётся в разделе/спросит."""
    from sreda.config import settings as sm
    cap = {}
    freddie = _Chat("freddie", classify="task", bound_capture=cap,
                    responses=[AIMessage(content="не умею это"), AIMessage(content="готово")])
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "t")  # Ф4: канареечный список
    sm.get_settings.cache_clear()
    _turn(freddie, thread="guard-rec", text="погода завтра")  # отказ → guard, но A4 НЕ расширяет домены
    last_bound = cap["freddie"][-1]            # bind 2-го прохода (после guard-nudge, БЕЗ видения доменов)
    assert "recall_memory" not in last_bound   # #267 A4: read-домен НЕ расширен (роутер побеждает)
    assert "add_task" not in last_bound        # write tasks НЕ расширен (write-гейт цел)


def test_domain_scope_execute_canary_excluded_is_shadow(install, monkeypatch):
    """Ф4 КАНАРЕЙКА: mode=execute, но тенант НЕ в списке → ведёт себя как shadow (фильтр НЕ применён,
    add_task на месте). Глобальный flip execute не трогает тех, кого нет в канареечном списке."""
    from sreda.config import settings as sm
    cap = {}
    freddie = _Chat("freddie", classify="task", bound_capture=cap, responses=[AIMessage(content="ок")])
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "other")  # "t" НЕ в списке
    sm.get_settings.cache_clear()
    _turn(freddie, thread="canary-excl", text="напомни купить молоко")
    assert "add_task" in cap["freddie"][-1]  # эффективный shadow — фильтр НЕ применён


def test_domain_scope_execute_canary_wildcard(install, monkeypatch):
    """Ф4 КАНАРЕЙКА: mode=execute + список='*' → execute на всех (фильтр применён, add_task срезан)."""
    from sreda.config import settings as sm
    cap = {}
    freddie = _Chat("freddie", classify="task", bound_capture=cap, responses=[AIMessage(content="ок")])
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "*")  # все
    sm.get_settings.cache_clear()
    _turn(freddie, thread="canary-wild", text="напомни купить молоко")
    bound = cap["freddie"][-1]
    assert "schedule_reminder" in bound and "add_task" not in bound  # execute драйвит (фильтр применён)


# ───────────────────────── #247: кеш-дисциплина — директивы в ХВОСТ ─────────────────────────
def test_tail_directives_off_section_in_system_prompt(install, monkeypatch):
    """Флаг OFF (легаси): section-hint #215 дописан в СИСТЕМНЫЙ промпт (нестабильный префикс — как было)."""
    from sreda.config import settings as sm
    spcap = []
    freddie = _Chat("freddie", classify="task", responses=[AIMessage(content="ок")], sp_capture=spcap)
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.delenv("SREDA_REACT_TAIL_DIRECTIVES", raising=False)
    sm.get_settings.cache_clear()
    _turn(freddie, thread="td-off", text="покажи дела")
    assert any(_HINT_CHECKLIST in (s or "") for s in spcap), "OFF: section должен быть в системном промпте"


def test_tail_directives_on_stable_prefix_and_tail(install, monkeypatch):
    """Флаг ON (#247): системный промпт СТАБИЛЕН (без section), директива section — в ХВОСТЕ (последнее сообщение).
    Это и чинит кеш-префикс: начало запроса не меняется от текста пользователя."""
    from sreda.config import settings as sm
    msgcap, typecap = [], []
    freddie = _Chat("freddie", classify="task", responses=[AIMessage(content="ок")],
                    msgs_capture=msgcap, types_capture=typecap)
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_TAIL_DIRECTIVES", "1")
    sm.get_settings.cache_clear()
    _turn(freddie, thread="td-on", text="покажи дела")
    first = msgcap[0]                                   # content всех сообщений 1-го вызова
    assert _HINT_CHECKLIST not in (first[0] or ""), "ON: системный промпт (первое сообщение) должен быть БЕЗ section"
    assert _HINT_CHECKLIST in (first[-1] or ""), "ON: section-директива должна быть в ХВОСТЕ (последнее сообщение)"
    # R2 MINOR (оба Codex): хвост — РОЛЬ user (HumanMessage), не SystemMessage (защита от регресса роли)
    assert typecap[0][-1] == "HumanMessage", f"ON: хвостовая директива должна быть ролью user, не {typecap[0][-1]}"


def test_tail_directives_on_prefix_identical_across_sections(install, monkeypatch):
    """Флаг ON: системный промпт (первое сообщение) БАЙТ-В-БАЙТ одинаков для РАЗНЫХ разделов — кеш-префикс стабилен."""
    from sreda.config import settings as sm
    cap1, cap2 = [], []
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_TAIL_DIRECTIVES", "1")
    sm.get_settings.cache_clear()
    _turn(_Chat("f1", classify="task", responses=[AIMessage(content="ок")], msgs_capture=cap1),
          thread="td-p1", text="покажи дела")              # → checklists-директива
    _turn(_Chat("f2", classify="task", responses=[AIMessage(content="ок")], msgs_capture=cap2),
          thread="td-p2", text="покажи задачи")            # → tasks-директива
    assert cap1[0][0] == cap2[0][0], "ON: системный промпт должен быть идентичен независимо от раздела запроса"


def test_tail_directives_on_nudge_in_tail(install, monkeypatch):
    """#247 R1 (MINOR medium): guard-нудж тоже уходит в ХВОСТ — последнее сообщение 2-го прохода
    (после refusal → guard-recovery) содержит нудж."""
    from sreda.config import settings as sm
    msgcap = []
    freddie = _Chat("freddie", classify="task",
                    responses=[AIMessage(content="не умею это"), AIMessage(content="готово")],
                    msgs_capture=msgcap)
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_TAIL_DIRECTIVES", "1")
    sm.get_settings.cache_clear()
    _turn(freddie, thread="td-nudge", text="напомни купить молоко")
    assert len(msgcap) >= 2, "ожидался 2-й проход после guard-recovery"
    assert "выполни запрос" in (msgcap[1][-1] or ""), "guard-нудж должен быть в хвосте 2-го прохода (роль user)"


# ───────────────────────── #250: section-директива из РОУТЕРА на execute ─────────────────────────
def _exec_env(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "t")
    monkeypatch.setenv("SREDA_REACT_TAIL_DIRECTIVES", "1")


def test_250_execute_router_directive_no_checklist_conflict(install, monkeypatch):
    """#250: на execute директива берётся из РОУТЕРА. «Покажи список покупок» → shopping → директивы НЕТ →
    в сообщениях НЕТ checklists-подсказки (раньше сырой _section_hint давал checklists на слове «список»)."""
    from sreda.config import settings as sm
    msgcap = []
    freddie = _Chat("freddie", classify="task", responses=[AIMessage(content="ок")], msgs_capture=msgcap)
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    _exec_env(monkeypatch); sm.get_settings.cache_clear()
    _turn(freddie, thread="b250-shop", text="Покажи список покупок")
    allmsgs = " ".join(m for p in msgcap for m in p)
    assert _HINT_CHECKLIST not in allmsgs, "#250: на execute «список покупок» НЕ должно быть checklists-директивы"


def test_250_execute_checklists_directive_kept(install, monkeypatch):
    """#250: «покажи дела» на execute → роутер checklists → директива checklists ОСТАЁТСЯ (полезный кейс не сломан)."""
    from sreda.config import settings as sm
    msgcap = []
    freddie = _Chat("freddie", classify="task", responses=[AIMessage(content="ок")], msgs_capture=msgcap)
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    _exec_env(monkeypatch); sm.get_settings.cache_clear()
    _turn(freddie, thread="b250-del", text="покажи дела")
    allmsgs = " ".join(m for p in msgcap for m in p)
    assert _HINT_CHECKLIST in allmsgs, "#250: «покажи дела» должно сохранить checklists-директиву (роутер checklists)"


def test_250_disabled_keeps_legacy_section_hint(install, monkeypatch):
    """#250: на disabled (роутер не сужает) — легаси _section_hint цел («список покупок»→checklists как было)."""
    from sreda.config import settings as sm
    msgcap = []
    freddie = _Chat("freddie", classify="task", responses=[AIMessage(content="ок")], msgs_capture=msgcap)
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.delenv("SREDA_REACT_DOMAIN_SCOPE_MODE", raising=False)  # disabled
    monkeypatch.setenv("SREDA_REACT_TAIL_DIRECTIVES", "1")
    sm.get_settings.cache_clear()
    _turn(freddie, thread="b250-dis", text="Покажи список покупок")
    allmsgs = " ".join(m for p in msgcap for m in p)
    assert _HINT_CHECKLIST in allmsgs, "#250 disabled: легаси _section_hint должен остаться (checklists на «список»)"


def test_250_execute_guard_2pass_no_checklist(install, monkeypatch):
    """#250 R1 (MINOR medium): фикс держится и на guard-2-проходе. execute «список покупок» + отказ → guard →
    НИ на одном из проходов нет checklists-директивы (router_allowed жив, _last_human_text исходный)."""
    from sreda.config import settings as sm
    msgcap = []
    freddie = _Chat("freddie", classify="task",
                    responses=[AIMessage(content="не умею это"), AIMessage(content="готово")],
                    msgs_capture=msgcap)
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    _exec_env(monkeypatch); sm.get_settings.cache_clear()
    _turn(freddie, thread="b250-guard", text="Покажи список покупок")
    assert len(msgcap) >= 2, "ожидался 2-й проход после guard"
    for i, p in enumerate(msgcap):
        assert _HINT_CHECKLIST not in " ".join(p), f"#250: checklists-директива не должна появляться (проход {i})"


def test_250_shadow_keeps_legacy_section_hint(install, monkeypatch):
    """#250 R1 (MINOR high): в shadow router_allowed=None → легаси _section_hint цел («список покупок»→checklists)."""
    from sreda.config import settings as sm
    msgcap = []
    freddie = _Chat("freddie", classify="task", responses=[AIMessage(content="ок")], msgs_capture=msgcap)
    install(on=True, deepseek=_Chat("deepseek"), invoked={})
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "shadow")
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "t")
    monkeypatch.setenv("SREDA_REACT_TAIL_DIRECTIVES", "1")
    sm.get_settings.cache_clear()
    _turn(freddie, thread="b250-shadow", text="Покажи список покупок")
    allmsgs = " ".join(m for p in msgcap for m in p)
    assert _HINT_CHECKLIST in allmsgs, "#250 shadow: легаси _section_hint должен остаться (checklists на «список»)"


# ───────────────────────── #256: отдельный короткий таймаут chat/fact ─────────────────────────
def test_256_chat_fact_short_timeout_task_unchanged(install, monkeypatch):
    """#256: chat/fact-вызовы под КОРОТКИМ таймаутом (react_chat_llm_timeout_sec); task — под общим 60с."""
    from sreda.config import settings as sm
    cap = []
    def _cap(runnable, msgs, timeout_seconds=None):
        cap.append(timeout_seconds)
        return AIMessage(content="ок")
    monkeypatch.setattr(react_loop, "invoke_with_per_call_timeout", _cap)
    monkeypatch.setenv("SREDA_REACT_LLM_TIMEOUT_SEC", "60")
    monkeypatch.setenv("SREDA_REACT_CHAT_LLM_TIMEOUT_SEC", "12")
    sm.get_settings.cache_clear()
    # chat/fact ход → короткий таймаут
    install(on=True, deepseek=_Chat("deepseek", responses=[AIMessage(content="ок")]), invoked={})
    _turn(_Chat("freddie", classify="chat"), thread="b256-chat", text="расскажи анекдот")
    assert cap and cap[0] == 12.0, f"chat/fact таймаут должен быть 12с, был {cap[:2]}"
    # task ход → общий 60с
    cap.clear()
    install(on=True, deepseek=_Chat("deepseek2"), invoked={})
    _turn(_Chat("freddie2", classify="task", responses=[AIMessage(content="готово")]),
          thread="b256-task", text="покажи мои задачи")
    assert cap and cap[0] == 60.0, f"task таймаут должен быть 60с, был {cap[:2]}"


def test_256_default_when_env_unset(install, monkeypatch):
    """#256: env НЕ задан → Field-дефолт 15с применяется к chat/fact (R1 субагент MINOR — ассерт значения)."""
    from sreda.config import settings as sm
    cap = []
    def _cap(runnable, msgs, timeout_seconds=None):
        cap.append(timeout_seconds)
        return AIMessage(content="ок")
    monkeypatch.setattr(react_loop, "invoke_with_per_call_timeout", _cap)
    monkeypatch.delenv("SREDA_REACT_CHAT_LLM_TIMEOUT_SEC", raising=False)
    sm.get_settings.cache_clear()
    install(on=True, deepseek=_Chat("deepseek", responses=[AIMessage(content="ок")]), invoked={})
    _turn(_Chat("freddie", classify="chat"), thread="b256-def", text="как дела")
    assert cap and cap[0] == 15.0, f"env не задан → дефолт 15с, был {cap[:2]}"
    sm.get_settings.cache_clear()


def test_256_misconfig_does_not_crash(install, monkeypatch):
    """#256: мусор в env (сломанный деплой) → get_settings падает глобально → ход НЕ падает (safe-reply)."""
    from sreda.config import settings as sm
    monkeypatch.setenv("SREDA_REACT_CHAT_LLM_TIMEOUT_SEC", "не-число")
    sm.get_settings.cache_clear()
    install(on=True, deepseek=_Chat("deepseek", responses=[AIMessage(content="ок")]), invoked={})
    # не должно бросить — ход завершается (safe-reply при глобальном сбое конфига)
    r = _turn(_Chat("freddie", classify="chat"), thread="b256-misc", text="как дела")
    assert r is not None
    sm.get_settings.cache_clear()
