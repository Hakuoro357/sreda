"""#383 Ф2 — проводка SGR-шага в chat-узел за флагом (план plans/383-sgr-final.md §2B/§4/§5/§6).

RED-до-кода. Покрытие (пункты приёмки §10):
- п.1: матрица OFF → байт-в-байт, react_sgr не вызывается/не импортируется, sgr-поля в трейсе нет;
- п.2: гейт РЕАЛЬНО активируется (в т.ч. на РЕАЛЬНОМ unified bound);
- п.5: сбой любой точки SGR-участка → легаси ТЕМ ЖЕ проходом, фолбэк-промпт = OFF-промпту;
- п.6: контракт возврата (инкремент проходов виден по call_index второй chat-записи);
- п.7: one-shot директивы → SGR неактивен (юнит гейт-хелпера `_sgr_gate_reason`);
- п.9: пауза, открытая SGR-ходом, доживает выключение флага до resume;
- п.10: sgr-трейс PII-free, per-attempt учёт стоимости;
- Opus Ф1 MINOR#3: wire-форма по ФАКТИЧЕСКИ вызванному провайдеру (фолбэк → envelope Осы).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop

_CHECKLIST_TOOLS = ["create_checklist", "show_checklist", "list_checklists",
                    "add_checklist_items", "delete_checklist_item"]
_OTHER_TOOLS = ["list_reminders", "schedule_reminder", "add_task",
                "recall_memory", "need_family", "ask_human"]
_WEB_TOOLS = ["web_search", "fetch_url", "get_weather"]

CHECKLIST_TEXT = "покажи список дел"

# Реальная фабрика инструментов — ДО фикстур (install подменяет атрибут модуля)
_REAL_BUILD_SLICE = react_loop.build_slice_tools


def _mk_tool(name, invoked):
    def _f(q: str = ""):
        invoked[name] = invoked.get(name, 0) + 1
        return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=f"Инструмент {name}.")


def _act_json(action: str, q: str = "x") -> str:
    return json.dumps({"kind": "act", "situation": "Просьба по спискам.",
                       "enough_data": True, "tool": {"action": action, "args": {"q": q}}},
                      ensure_ascii=False)


def _finish_json(reply: str = "Готово.") -> str:
    return json.dumps({"kind": "finish", "situation": "Задача выполнена.",
                       "task_completed": True, "reply": reply}, ensure_ascii=False)


class _CapTrace:
    """Фейк _trace: включён, копит llm_calls из persist_trace_finish; остальное — no-op."""

    def __init__(self):
        self.llm_calls: list[dict] = []

    def trace_enabled(self):
        return True

    def collect_tool_calls(self, *a, **k):
        return []

    def persist_trace_finish(self, **kwargs):
        self.llm_calls.extend(kwargs.get("llm_calls") or [])

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _SgrChat:
    """Фейк планировщика: bind_tools → легаси (капчер сообщений/набора);
    bind(**kwargs) → structured (капчер kwargs/сообщений; канированные ответы/исключения)."""

    def __init__(self, label, *, legacy_msgs=None, legacy_bound=None,
                 sgr_binds=None, sgr_msgs=None, sgr_responses=None,
                 legacy_responses=None):
        self.label = label
        self._legacy_msgs = legacy_msgs
        self._legacy_bound = legacy_bound
        self._sgr_binds = sgr_binds
        self._sgr_msgs = sgr_msgs
        self._sgr_responses = list(sgr_responses or [])
        self._legacy_responses = list(legacy_responses or [])

    async def ainvoke(self, _msgs):
        return AIMessage(content="task")  # интент-классификатор preflight (fail-open не нужен)

    def bind_tools(self, tools):
        if self._legacy_bound is not None:
            self._legacy_bound.append(sorted(getattr(t, "name", "?") for t in tools))
        outer = self

        def _inv(_msgs):
            if outer._legacy_msgs is not None:
                outer._legacy_msgs.append(list(_msgs))
            if outer._legacy_responses:
                return outer._legacy_responses.pop(0)
            return AIMessage(content="legacy-" + outer.label)
        return RunnableLambda(_inv)

    def bind(self, **kwargs):
        if self._sgr_binds is not None:
            self._sgr_binds.append(kwargs)
        outer = self

        def _inv(_msgs):
            if outer._sgr_msgs is not None:
                outer._sgr_msgs.append(list(_msgs))
            if not outer._sgr_responses:
                return AIMessage(content=_finish_json())
            r = outer._sgr_responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return AIMessage(content=r) if isinstance(r, str) else r
        return RunnableLambda(_inv)


@pytest.fixture
def install(monkeypatch):
    from sreda.config import settings as settings_mod
    state = {}

    def _install(*, sgr_flag=False, sgr_tenants="", tools=None):
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_TENANTS", "*")
        monkeypatch.setenv("SREDA_SGR_PLANNER_ENABLED", "1" if sgr_flag else "0")
        monkeypatch.setenv("SREDA_SGR_PLANNER_TENANTS", sgr_tenants)
        settings_mod.get_settings.cache_clear()
        inv = {}
        names = tools if tools is not None else (_CHECKLIST_TOOLS + _OTHER_TOOLS + _WEB_TOOLS)
        monkeypatch.setattr(react_loop, "build_slice_tools",
                            lambda *a, **k: [_mk_tool(n, inv) for n in names])
        trace = _CapTrace()
        monkeypatch.setattr(react_loop, "_trace", trace)
        state["inv"], state["trace"] = inv, trace
        return state

    yield _install
    settings_mod.get_settings.cache_clear()


def _turn(llm, *, thread, text, tenant="t-canary", fallback=None):
    return asyncio.run(react_loop.handle_turn(
        session=None, tenant_id=tenant, user_id="u", thread_id=thread,
        llm=llm, user_text=text, inbound_message_id=f"{thread}:{text[:10]}",
        channel="react", resume_only=False, expected_confirm_id="",
        provider_key="inception-mercury2", fallback_llm=fallback))


def _chat_calls(trace):
    return [c for c in trace.llm_calls if c.get("phase") == "chat"]


# ─────────────── п.1: матрица OFF → байт-в-байт, react_sgr не зовётся ───────────────


@pytest.mark.parametrize("flag, tenants", [
    (False, ""),                # ENABLED=0
    (True, "t-other"),          # тенант не в списке
])
def test_sgr_gate_off_identical(install, monkeypatch, flag, tenants):
    st = install(sgr_flag=flag, sgr_tenants=tenants)
    import sreda.runtime.react_sgr as react_sgr
    monkeypatch.setattr(react_sgr, "compute_sgr_tools",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен зваться")))
    legacy_msgs, sgr_binds = [], []
    llm = _SgrChat("m", legacy_msgs=legacy_msgs, sgr_binds=sgr_binds)
    res = _turn(llm, thread=f"off-{flag}-{tenants}", text=CHECKLIST_TEXT)
    assert str(res)
    assert sgr_binds == []                       # structured-вызова не было
    assert legacy_msgs, "легаси-вызов обязан был случиться"
    for c in _chat_calls(st["trace"]):
        assert "sgr" not in c                    # ни одного sgr-поля в трейсе


def test_sgr_off_no_import(install, monkeypatch):
    # OFF-путь не должен даже импортировать react_sgr (изоляция OFF, R1 sol M7).
    # CR R2 Opus MINOR#2: ловим ОБЕ формы импорта (`import ... react_sgr` даёт name с
    # 'react_sgr'; `from sreda.runtime import react_sgr` — name='sreda.runtime' +
    # fromlist=['react_sgr']) И проверяем sys.modules по ПОЛНОМУ имени после хода.
    import sys as _sys
    _sys.modules.pop("sreda.runtime.react_sgr", None)
    install(sgr_flag=False)
    import builtins
    real_import = builtins.__import__

    def _guard(name, globals=None, locals=None, fromlist=(), level=0):
        assert "react_sgr" not in name, "OFF-путь импортировал react_sgr (import-форма)"
        assert "react_sgr" not in (fromlist or ()), \
            "OFF-путь импортировал react_sgr (from-форма)"
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", _guard)
    res = _turn(_SgrChat("m"), thread="off-noimp", text=CHECKLIST_TEXT)
    assert str(res)
    assert "sreda.runtime.react_sgr" not in _sys.modules, \
        "react_sgr оказался в sys.modules при OFF"


def test_sgr_inactive_empty_slice_label(install, monkeypatch):
    # CR R2 Opus MINOR#3: пустой срез → inactive_reason='empty_slice' (НЕ 'union_size')
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    import sreda.runtime.react_sgr as react_sgr
    monkeypatch.setattr(react_sgr, "compute_sgr_tools", lambda *a, **k: [])
    _turn(_SgrChat("m"), thread="empty-slice", text=CHECKLIST_TEXT)
    sgr = [c["sgr"] for c in _chat_calls(st["trace"]) if c.get("sgr")][0]
    assert sgr["active"] is False and sgr["inactive_reason"] == "empty_slice"


@pytest.mark.parametrize("text", [
    "поставь напоминание на завтра в 9 утра",   # чужой домен
    "покажи список кино и покупки",             # CR R1: явная shopping-группа рядом с чеклистовой
])
def test_sgr_inactive_on_non_checklist_turn(install, text):
    # флаг+тенант ВКЛ, но ход не «чисто чеклистовый» → SGR неактивен, легаси; причина в трейсе
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    sgr_binds, legacy_msgs = [], []
    llm = _SgrChat("m", sgr_binds=sgr_binds, legacy_msgs=legacy_msgs)
    _turn(llm, thread=f"nonchk-{len(text)}", text=text)
    assert sgr_binds == [] and legacy_msgs
    sgr_fields = [c["sgr"] for c in _chat_calls(st["trace"]) if c.get("sgr")]
    assert sgr_fields and sgr_fields[0]["active"] is False
    assert sgr_fields[0]["inactive_reason"]


# ─────────────── п.2: гейт активируется; успешный SGR-ход ───────────────


def test_sgr_gate_activates_and_executes_act(install):
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    sgr_binds, sgr_msgs, legacy_msgs = [], [], []
    llm = _SgrChat("m", sgr_binds=sgr_binds, sgr_msgs=sgr_msgs, legacy_msgs=legacy_msgs,
                   sgr_responses=[_act_json("show_checklist", q="продукты"), _finish_json()])
    res = _turn(llm, thread="act1", text=CHECKLIST_TEXT)
    assert str(res) == "Готово."
    assert st["inv"].get("show_checklist") == 1        # act реально исполнен run_tools
    assert len(sgr_binds) == 2 and legacy_msgs == []   # оба прохода — SGR, легаси не звался
    rf = sgr_binds[0]["response_format"]["json_schema"]
    assert rf["name"] == "sgr_step" and "anyOf" in rf["schema"]   # flat (Mercury)
    calls = _chat_calls(st["trace"])
    assert calls[0]["sgr"]["active"] is True
    assert calls[0]["sgr"]["kind"] == "act" and calls[0]["sgr"]["action"] == "show_checklist"
    # п.6 (контракт возврата): второй проход существует, его call_index=1 → инкремент был,
    # и ход ЗАВЕРШИЛСЯ (анти-петля жива)
    assert calls[1]["call_index"] == 1


def test_sgr_gate_activates_on_real_unified_bound(install, monkeypatch):
    # п.2 «эксперимент не вакуумный»: РЕАЛЬНЫЙ build_slice_tools (EntitlementGate заглушен),
    # реальная unified-политика → гейт истинен, в срезе чеклисты+web и НЕТ чужих доменов
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    monkeypatch.setattr(react_loop, "build_slice_tools", _REAL_BUILD_SLICE)
    from sreda.services import entitlement_gate as eg
    monkeypatch.setattr(eg.EntitlementGate, "check",
                        lambda self, tid: eg.GateResult(
                            allowed=True, reason="ok", plan_key="probe", is_grandfathered=True))
    sgr_binds = []
    llm = _SgrChat("m", sgr_binds=sgr_binds, sgr_responses=[_finish_json("Вот список.")])
    res = _turn(llm, thread="realbound", text=CHECKLIST_TEXT)
    # Ассертим АКТИВАЦИЮ и состав схемы (реальные инструменты без БД не исполнить;
    # финальный текст может уйти легаси-форс-проходом freshness-гейта — это не про гейт)
    assert str(res)
    assert len(sgr_binds) >= 1, "гейт обязан активироваться на реальном bound"
    sch = sgr_binds[0]["response_format"]["json_schema"]["schema"]
    branches = [b["properties"]["action"]["const"]
                for b in sch["anyOf"][0]["properties"]["tool"]["anyOf"]]
    assert any("checklist" in n for n in branches) and "web_search" in branches
    assert all(n not in branches for n in ("schedule_reminder", "add_task", "ask_human"))
    _ = st


def test_sgr_availability_tail_from_slice(install):
    # §4: availability-подсказка SGR-вызова — из sgr_tools, не из полного bound
    install(sgr_flag=True, sgr_tenants="t-canary")
    sgr_msgs = []
    llm = _SgrChat("m", sgr_msgs=sgr_msgs, sgr_responses=[_finish_json()])
    _turn(llm, thread="avail", text=CHECKLIST_TEXT)
    joined = "\n".join(str(getattr(m, "content", "")) for m in sgr_msgs[0])
    # availability-строка (а не весь промпт: статичная персона легитимно описывает ядро)
    avail_line = next(ln for ln in joined.splitlines()
                      if "доступны инструменты" in ln)
    assert "show_checklist" in avail_line
    assert "schedule_reminder" not in avail_line   # чужой домен не обещаем


# ─────────────── п.5: деградация в легаси ТЕМ ЖЕ проходом ───────────────


def test_sgr_invalid_falls_back_to_legacy(install):
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    sgr_binds, legacy_msgs = [], []
    llm = _SgrChat("m", sgr_binds=sgr_binds, legacy_msgs=legacy_msgs,
                   sgr_responses=["это не json"])
    res = _turn(llm, thread="inv1", text=CHECKLIST_TEXT)
    assert str(res).startswith("legacy-")
    assert len(sgr_binds) >= 1 and len(legacy_msgs) >= 1   # тот же ход доехал легаси
    sgr = _chat_calls(st["trace"])[0]["sgr"]
    assert sgr["fallback_reason"] == "invalid_response"


def test_sgr_fallback_prompt_equals_off(install):
    # §4: фолбэк-промпт байт-в-байт равен OFF-промпту (легаси не видит анкету/срез)
    off_msgs, fb_msgs = [], []
    install(sgr_flag=False)
    _turn(_SgrChat("m", legacy_msgs=off_msgs), thread="pp-off", text=CHECKLIST_TEXT)
    install(sgr_flag=True, sgr_tenants="t-canary")
    _turn(_SgrChat("m", legacy_msgs=fb_msgs, sgr_responses=["мусор"]),
          thread="pp-fb", text=CHECKLIST_TEXT)
    a = [(type(m).__name__, str(m.content)) for m in off_msgs[0]]
    b = [(type(m).__name__, str(m.content)) for m in fb_msgs[0]]
    assert a == b


@pytest.mark.parametrize("target", [
    "compute_sgr_tools", "build_wire_schema", "parse_sgr_reply", "decision_to_aimessage",
])
def test_sgr_failpoints_parametrized(install, monkeypatch, target):
    # §5: исключение в ЛЮБОЙ точке SGR-участка → легаси тем же проходом, не safe_reply
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    import sreda.runtime.react_sgr as react_sgr
    monkeypatch.setattr(react_sgr, target,
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    legacy_msgs = []
    llm = _SgrChat("m", legacy_msgs=legacy_msgs,
                   sgr_responses=[_act_json("show_checklist")])
    res = _turn(llm, thread=f"fp-{target}", text=CHECKLIST_TEXT)
    assert str(res).startswith("legacy-")
    assert legacy_msgs
    sgr = (_chat_calls(st["trace"])[0].get("sgr")) or {}
    assert sgr.get("fallback_reason") or sgr.get("inactive_reason")


def test_sgr_fallback_shape_by_provider(install):
    # Opus Ф1 MINOR#3: транзиентный сбой primary → structured-Оса с ЕЁ формой (envelope).
    # Оса отвечает ЧТЕНИЕМ (иначе freshness-guard #356 справедливо форсит перечитку и
    # ставит нудж → SGR деактивируется — отдельно проверено в п.7-тесте).
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    prim_binds, fb_binds = [], []
    primary = _SgrChat("m", sgr_binds=prim_binds,
                       sgr_responses=[RuntimeError("connection reset"),
                                      _finish_json("Готово после чтения.")])
    osa = _SgrChat("osa", sgr_binds=fb_binds,
                   sgr_responses=[json.dumps({
                       "situation": "Нужно прочитать список.",
                       "step": {"kind": "act", "enough_data": True,
                                "tool": {"action": "show_checklist", "args": {"q": "дела"}}}},
                       ensure_ascii=False)])
    res = _turn(primary, thread="shape-fb", text=CHECKLIST_TEXT, fallback=osa)
    assert str(res) == "Готово после чтения."
    assert st["inv"].get("show_checklist") == 1     # act Осы исполнен
    prim_schema = prim_binds[0]["response_format"]["json_schema"]["schema"]
    fb_schema = fb_binds[0]["response_format"]["json_schema"]["schema"]
    assert "anyOf" in prim_schema and prim_schema.get("type") != "object"       # flat (Mercury)
    assert fb_schema.get("type") == "object" and "step" in fb_schema["properties"]  # envelope
    # CR R1 sol MINOR: structured-фолбэк виден в общих trace-полях (не скрыт)
    c0 = _chat_calls(st["trace"])[0]
    assert c0["fallback_fired"] is True and c0["retries"] == 1
    assert c0["selected_provider"] == "groq-gpt-oss-120b"
    assert c0["primary_provider_key"] == "inception-mercury2"
    assert c0["primary_error"] == "RuntimeError"


# ─────────────── п.7: one-shot директивы → SGR неактивен (юнит гейт-хелпера) ───────────────


def test_sgr_inactive_on_one_shot_directives():
    f = react_loop._sgr_gate_reason
    base = dict(unified_execute=True,
                allowed_read=frozenset({"checklists", "web"}), allowed_write=frozenset(),
                guard_nudge="", stale_pause_note="", provider_key="inception-mercury2")
    assert f(**base) is None
    assert f(**{**base, "guard_nudge": "перечитай список"}) == "one_shot_directive_pending"
    assert f(**{**base, "stale_pause_note": "вчера я спрашивала"}) == "one_shot_directive_pending"
    assert f(**{**base, "allowed_read": frozenset({"checklists", "reminders"})}) == "domain_mix"
    assert f(**{**base, "allowed_read": frozenset({"web"})}) == "domain_mix"
    # shopping — допустимая read-попутчица неоднозначной кюс-группы «список» (Ф2-калибровка
    # на живой политике: «покажи список дел» даёт {checklists, shopping, web}) — НО только
    # при provenance попутчицы в ТЕКСТЕ (CR R1 sol+terra MAJOR)
    ar_shop = frozenset({"checklists", "shopping", "web"})
    assert f(**{**base, "allowed_read": ar_shop,
                "user_text": "покажи список дел"}) is None
    # явная shopping-группа рядом с чеклистовой — SGR не умеет покупки → легаси
    assert f(**{**base, "allowed_read": ar_shop,
                "user_text": "покажи список кино и покупки"}) == "domain_mix"
    # route-домен shopping (явное «покупки») → легаси
    assert f(**{**base, "allowed_read": ar_shop,
                "user_text": "что в списке покупок"}) == "domain_mix"
    # пустой текст при shopping в ar → fail-closed (companionship не доказана)
    assert f(**{**base, "allowed_read": ar_shop, "user_text": ""}) == "domain_mix"
    # но ЗАПИСЬ вне чеклистов — не наш ход
    assert f(**{**base, "allowed_write": frozenset({"shopping"})}) == "domain_mix"
    assert f(**{**base, "allowed_write": frozenset({"checklists"})}) is None
    assert f(**{**base, "provider_key": "unknown-provider"}) == "provider_unsupported"
    assert f(**{**base, "unified_execute": False}) == "not_unified"


# ─────────────── п.9: пауза SGR-хода доживает выключение флага ───────────────


def test_sgr_pause_survives_flag_off(install, monkeypatch):
    from sreda.config import settings as settings_mod
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    llm = _SgrChat("m", sgr_responses=[
        _act_json("delete_checklist_item", q="хлеб"),   # write вне allowed → кандидат+confirm
    ], legacy_responses=[AIMessage(content="после resume")])
    res1 = _turn(llm, thread="pause1", text=CHECKLIST_TEXT)
    assert getattr(res1, "awaiting_confirm", False) is True, str(res1)
    assert st["inv"].get("delete_checklist_item") is None   # до «да» мутации нет (п.11 парность)
    # выключаем флаг ДО resume
    monkeypatch.setenv("SREDA_SGR_PLANNER_ENABLED", "0")
    settings_mod.get_settings.cache_clear()
    res2 = _turn(llm, thread="pause1", text="да")
    assert st["inv"].get("delete_checklist_item") == 1      # пауза дожила, исполнена штатно
    assert str(res2)


# ─────────────── п.10: PII-free sgr-трейс + per-attempt учёт ───────────────


def test_sgr_trace_no_free_text_in_llm_calls(install):
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    llm = _SgrChat("m", sgr_responses=[
        _act_json("show_checklist", q="секретные покупки"), _finish_json("Вот.")])
    _turn(llm, thread="pii", text="покажи мой список дел")
    sgr = _chat_calls(st["trace"])[0]["sgr"]
    dumped = json.dumps(sgr, ensure_ascii=False)
    assert "секретн" not in dumped.lower()
    assert set(sgr) <= {"active", "inactive_reason", "fallback_reason", "kind", "action",
                        "enough_data", "task_completed", "situation_len", "args_hash"}


def test_sgr_double_structured_failure_keeps_telemetry(install):
    # CR R2 sol+terra MINOR: primary structured упал, Оса structured невалидна → легаси
    # успешен; телеметрия structured-попыток НЕ теряется (retries/primary_error видны)
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    primary = _SgrChat("m", sgr_responses=[RuntimeError("boom-primary")])
    osa = _SgrChat("osa", sgr_responses=["мусор от осы"])
    res = _turn(primary, thread="dblfail", text=CHECKLIST_TEXT, fallback=osa)
    assert str(res).startswith("legacy-")
    c0 = _chat_calls(st["trace"])[0]
    assert c0["fallback_fired"] is True and c0["retries"] == 1
    assert c0["primary_error"] == "RuntimeError"
    assert c0["sgr"]["fallback_reason"] == "invalid_response"


def test_sgr_per_attempt_usage(install, monkeypatch):
    # invalid → легаси: учтены ОБЕ завершённые попытки (structured + легаси)
    install(sgr_flag=True, sgr_tenants="t-canary")
    recorded = []
    monkeypatch.setattr(react_loop, "_record_react_usage",
                        lambda **k: recorded.append(k["provider_key"]))
    llm = _SgrChat("m", sgr_responses=["мусор"])
    _turn(llm, thread="usage1", text=CHECKLIST_TEXT)
    assert recorded.count("inception-mercury2") >= 2  # structured-попытка + легаси-попытка


# ─────────────── п.6: машинный dict-пин контракта возврата узла (класс #74/g-042) ───────────────


def _sgr_graph_state(inv, sgr_responses, *, thread, text="покажи список дел",
                     turn_pass_count=0, guard_nudge="", stale_pause_note=""):
    """Прямой прогон _build_graph → g.invoke (паттерн test_preflight_trace_fields):
    возврат узла chat аккумулируется в состоянии графа — читаем его напрямую."""
    llm = _SgrChat("g", sgr_responses=list(sgr_responses))
    tools = [_mk_tool(n, inv) for n in _CHECKLIST_TOOLS]
    g = react_loop._build_graph(
        llm, tools, tenant_id="t-canary", user_id="u", today_str="2026-07-17",
        session=None, provider_key="inception-mercury2", preflight_enabled=True)
    return g.invoke(
        {"messages": [HumanMessage(text)], "turn_key": f"tk-{thread}",
         "active_families": ["checklists"], "guard_attempted_families": [],
         "turn_pass_count": turn_pass_count, "guard_nudge": guard_nudge,
         "stale_pause_note": stale_pause_note, "wrote_unkeyed": False, "intent": "task",
         "unified_execute": True,
         "router_allowed_read_domains": ["checklists", "web"],
         "router_allowed_write_domains": [],
         "intent_meta": {"source": "unified", "must_task": False, "classifier_raw": None}},
        {"configurable": {"thread_id": f"sgr-ret-{thread}"}})


def test_sgr_return_contract(install):
    # приёмка п.6: SGR-успешный возврат несёт инкремент turn_pass_count (ЕДИНСТВЕННЫЙ кормилец
    # анти-петли, route :3900/:3912) И ОБА one-shot-сброса ключами. Прямой пин возврата узла.
    install(sgr_flag=True, sgr_tenants="t-canary")
    inv = {}
    # act(read) → finish: ДВА chat-прохода → инкремент ПО ПРОХОДУ (не константа)
    res = _sgr_graph_state(
        inv, [_act_json("show_checklist", q="дела"), _finish_json("Готово.")], thread="two")
    assert res["turn_pass_count"] == 2                       # 0 → +1 → +1
    assert "guard_nudge" in res and res["guard_nudge"] == ""        # one-shot consume
    assert "stale_pause_note" in res and res["stale_pause_note"] == ""
    chat_calls = [c for c in res["llm_calls"] if c.get("phase") == "chat"]
    assert len(chat_calls) == 2 and all(c["sgr"]["active"] is True for c in chat_calls)
    assert inv.get("show_checklist") == 1                    # act реально исполнен run_tools
    # инкремент = prev+1 (НЕ reset-в-число проходов): старт с 3, те же 2 прохода → 5
    res2 = _sgr_graph_state(
        inv, [_act_json("show_checklist", q="дела"), _finish_json("Готово.")], thread="from3",
        turn_pass_count=3)
    assert res2["turn_pass_count"] == 5


# ─────────────── п.11: confirm-парность на ВСЕХ write-мутациях среза ───────────────


# add_checklist_items ИСКЛЮЧЁН из confirm-parity с #392 (см. test_sgr_add_checklist_items_autoexec_392
# ниже): он в _UNIFIED_AUTOEXEC_WRITE_TOOLS → на read-ходе aw=∅ биндится ПРЯМЫМ (аддитивно/видимо/
# обратимо), а не кандидатом. Деструктив/создание списка (delete/create) confirm сохраняют.
@pytest.mark.parametrize("write_tool", ["create_checklist", "delete_checklist_item"])
def test_sgr_confirm_parity(install, write_tool):
    """приёмка п.11: given чеклистовая мутация ВНЕ autoexec-реестра, для которой легаси требует
    подтверждение (на read-ходе allowed_write=∅ → write = кандидат под _generic_confirm_wrap),
    when SGR её выбирает, then пауза ДО «да» и НОЛЬ мутаций в состоянии до подтверждения.
    Юнит меряет ДИСПЕТЧ (мутация-функция не вызвана до confirm); фактическую неизменность
    ДАННЫХ в БД на всём голд-наборе меряет Ф3-живой прогон (self-verify-before-owner)."""
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    llm = _SgrChat("m", sgr_responses=[_act_json(write_tool, q="нечто")],
                   legacy_responses=[AIMessage(content="после resume")])
    res = _turn(llm, thread=f"parity-{write_tool}", text=CHECKLIST_TEXT)
    assert getattr(res, "awaiting_confirm", False) is True, str(res)   # пауза до «да»
    assert st["inv"].get(write_tool) is None                          # 0 мутаций до подтверждения


def test_sgr_add_checklist_items_autoexec_392(install):
    """#392 (кросс-фича с #383 п.11): add_checklist_items в autoexec-реестре → на read-ходе
    «покажи список дел» (aw=∅) SGR-выбор биндится ПРЯМЫМ, БЕЗ confirm-паузы. Осознанный
    tradeoff (позиция владельца #389/#392: добавление аддитивно/видимо/обратимо). Деструктив
    чек-листов confirm сохраняет (test_sgr_confirm_parity + test_sgr_pause_survives_flag_off)."""
    st = install(sgr_flag=True, sgr_tenants="t-canary")
    llm = _SgrChat("m", sgr_responses=[_act_json("add_checklist_items", q="нечто"),
                                       _finish_json("Добавила.")])
    res = _turn(llm, thread="autoexec-addcl", text=CHECKLIST_TEXT)
    assert getattr(res, "awaiting_confirm", False) is not True, str(res)  # НЕТ паузы
    assert st["inv"].get("add_checklist_items") == 1                      # исполнен прямо


# ─────────────── MINOR#1: golden-пин собранного OFF-промпта (анти-reorder хвоста) ───────────────


# Golden-снапшот human-строки легаси-сборки (вход детерминирован: фикс. время, overlay пуст,
# фикс. набор инструментов). Снят с фактического прогона 2026-07-17 (Ф2, коммит f08c9e3-фикс).
# Правишь сборку хвоста ОСОЗНАННО → обнови снапшот в ЭТОМ тесте с новой слома-проверкой.
_GOLDEN_OFF_HUMAN = (
    "покажи список дел\n\nСейчас 2026-07-17, четверг.\n\nГлавное: ответь человеку ПО СУЩЕСТВУ "
    "его запроса, ОПИРАЯСЬ на результаты инструментов этого хода — что инструмент реально "
    "сделал или нашёл, то и скажи; действий, которых в результатах нет, себе не приписывай. "
    "Если инструмент вернул отмену или «не делаю» — так и сообщи («отменила, ничего не делаю»), "
    "и ОСТАНОВИСЬ: НЕ переспрашивай и не предлагай сделать это снова. Не отвечай мимо запроса "
    "и не переспрашивай «что записать», если человек спросил о другом. Эту служебную заметку "
    "НЕ пересказывай в ответе. В этом ходе доступны инструменты: add_checklist_items, add_task, "
    "ask_human, create_checklist, delete_checklist_item, fetch_url, get_weather, "
    "list_checklists, need_family, schedule_reminder, show_checklist, web_search. Это про "
    "текущий ход, других инструментов здесь не зови; но способность к напоминаниям, задачам, "
    "спискам и памяти есть — не говори «не умею». Если человек хочет записать, напомнить или "
    "запомнить, а нужного инструмента здесь нет, не отказывай — коротко уточни недостающее; "
    "иначе просто ответь на его вопрос.\n\nВАЖНО: пользователь спрашивает про СПИСКИ ДЕЛ "
    "(раздел «Дела» = чек-листы со списками пунктов). Назван КОНКРЕТНЫЙ список по имени "
    "(напр. «список кино», «Поход») → show_checklist(list_id_or_title с этим именем): пункты "
    "ИМЕННО его, не обзор. Слово-раздел БЕЗ имени («дела», «список дел», «мои списки», «какие "
    "списки», «покажи все») → list_checklists (обзор со счётчиками). Покупки идут ОТДЕЛЬНО в "
    "list_shopping, это не чек-лист. НЕ показывай напоминания (list_reminders) и НЕ показывай "
    "задачи (list_tasks)."
)


def test_sgr_off_prompt_golden(install, monkeypatch):
    """CR R2 Opus MINOR#1 + слома-проверка кракена: пин = ТОЧНОЕ равенство ВСЕЙ human-строки
    (full-string equality) — любая молчаливая правка _assemble_msgs (перестановка блоков
    хвоста avail↔sec, изменение текста, перенос в system) краснит тест. Честно: пин ловит
    именно ИЗМЕНЕНИЕ строки; семантику блоков держат содержательные тесты B4/247/298.
    Детерминизм входа: время заморожено, персона-overlay пуст, фикс. набор инструментов."""
    monkeypatch.setattr(react_loop, "_now_tail_line", lambda: "Сейчас 2026-07-17, четверг.")
    monkeypatch.setenv("SREDA_REACT_TIME_IN_TAIL", "1")   # today_str="" → дата не в system
    st = install(sgr_flag=False)                          # OFF: легаси-сборка (unified path)
    from sreda.config import settings as settings_mod
    settings_mod.get_settings.cache_clear()
    off_msgs = []
    _turn(_SgrChat("m", legacy_msgs=off_msgs), thread="golden", text=CHECKLIST_TEXT)
    msgs = off_msgs[0]
    # структура: [System, Human] (свежий ход без истории)
    assert [type(m).__name__ for m in msgs] == ["SystemMessage", "HumanMessage"]
    # system = КАНОНИЧЕСКИЙ _system_prompt БЕЗ хвостовых директив (хвост НЕ в system)
    assert msgs[0].content == react_loop._system_prompt("", "")
    # ПОЛНОЕ равенство human-строки (не членство блоков)
    assert msgs[1].content == _GOLDEN_OFF_HUMAN
    _ = st
