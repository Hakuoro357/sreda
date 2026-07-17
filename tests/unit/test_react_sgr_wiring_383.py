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
from langchain_core.messages import AIMessage
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
    # OFF-путь не должен даже импортировать react_sgr (изоляция OFF, R1 sol M7)
    install(sgr_flag=False)
    import builtins
    real_import = builtins.__import__

    def _guard(name, *a, **k):
        assert "react_sgr" not in name, "OFF-путь импортировал react_sgr"
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _guard)
    res = _turn(_SgrChat("m"), thread="off-noimp", text=CHECKLIST_TEXT)
    assert str(res)


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


def test_sgr_per_attempt_usage(install, monkeypatch):
    # invalid → легаси: учтены ОБЕ завершённые попытки (structured + легаси)
    install(sgr_flag=True, sgr_tenants="t-canary")
    recorded = []
    monkeypatch.setattr(react_loop, "_record_react_usage",
                        lambda **k: recorded.append(k["provider_key"]))
    llm = _SgrChat("m", sgr_responses=["мусор"])
    _turn(llm, thread="usage1", text=CHECKLIST_TEXT)
    assert recorded.count("inception-mercury2") >= 2  # structured-попытка + легаси-попытка
