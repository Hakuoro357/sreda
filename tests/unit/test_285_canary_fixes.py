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

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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


def test_is_new_request_on_pause():
    """#316: детектор «новый запрос на паузе» — write-команда ИЛИ явный read-ЗАПРОС → True;
    ответ-уточнение (в т.ч. голый доменный ярлык) → False.

    R2 (оба Codex + субагент, MAJOR): ГЛАВНЫЙ класс ложных срабатываний — слот-ответы с доменным
    кюсом «покупк» на вопрос «в какой список?»/«куда добавить?» (канонический ответ = «покупки»).
    Голый кюс их путал с новым запросом → бросал живую паузу и терял «добавь молоко». Требуем
    маркер-запроса сверх кюса → слот-ответы False."""
    f = react_loop._is_new_request_on_pause
    # ── НОВЫЙ запрос (маркер-запроса + домен, либо write-императив) → True
    assert f("покажи покупки")             # read-запрос: маркер «покажи» + shopping
    assert f("добавь молоко в покупки")     # write-команда (императив)
    assert f("какие у меня дела")           # обзорный read-запрос (M1): «какие» + checklists
    assert f("покажи мои напоминания")      # «покажи» + reminders
    assert f("сколько у меня задач")        # WH «сколько» + tasks
    assert f("что в списке покупок")        # «что в» + checklists/shopping
    assert f("удали задачу про врача")      # write-императив «удали»
    # ── слот-ответы на «в какой список?»/«куда?» — доменный кюс ЕСТЬ, маркера НЕТ → False (R2 MAJOR)
    for ans in ("в покупки", "покупки", "в список покупок", "список покупок",
                "покупки пожалуйста", "в продукты", "в дела", "в рабочий", "в личный"):
        assert not f(ans), f"слот-ответ {ans!r} НЕ должен считаться новым запросом (бросил бы паузу)"
    # ── ответы на «во сколько?»/«что напомнить?» → False
    for ans in ("в 18:00", "завтра в 10", "через час", "в 6 вечера",
                "молоко", "купить хлеб", "мясо", "позвонить врачу", "дел"):
        assert not f(ans), f"ответ {ans!r} не должен считаться новым запросом"
    # ── WH-похожее слово без домена («какой-то») → False (маркер есть, кюса нет)
    assert not f("какой-то важный созвон")


def test_should_redirect_on_pause():
    """#316 R3/R4/R5: РЕАЛЬНАЯ функция решения (та же, что зовёт handle_turn — субагент R4 спец-дрейф #74,
    тест бьёт код, не реимплементацию). confirm-пауза: сигнал И НЕ эхо-подтверждение; ask_human: сигнал."""
    f = react_loop._should_redirect_on_pause
    # confirm: разговорные аффирмативы / голое эхо / ХВОСТОВЫЕ филлеры / пунктуация → НЕ redirect (A0 «нет»)
    for t in ("ок", "окей", "конечно", "давай", "хорошо", "ладно", "ну давай",
              "давай удаляй", "удаляй", "ок удаляй",
              "удали", "удалите", "ок удали", "ну давай удали", "да удали", "отмени",
              "удали пожалуйста", "ок удали пожалуйста", "удали,", "удали!!!", "тогда удали", "удали же"):
        assert not f(t, is_confirm_pause=True), f"confirm: {t!r} НЕ должен бросать живой confirm"
    # confirm: строгие да/нет → штатный resume
    for t in ("да", "нет", "подтверждаю", "отмена"):
        assert not f(t, is_confirm_pause=True), t
    # confirm: настоящий новый запрос (объект/read) → redirect (свежий ход)
    for t in ("удали задачу про врача", "удали покупки", "покажи покупки",
              "добавь молоко в покупки", "какие у меня дела"):
        assert f(t, is_confirm_pause=True), f"confirm: {t!r} — новый запрос, должен redirect"
    # ask_human: эхо-гейта НЕТ (у открытого вопроса нет да/нет-действия). Сигнал → redirect, слот-ответ → нет
    assert f("покажи покупки", is_confirm_pause=False)
    assert f("добавь молоко в покупки", is_confirm_pause=False)
    assert not f("в покупки", is_confirm_pause=False)   # слот-ответ «в какой список?»
    assert not f("в 18:00", is_confirm_pause=False)      # слот-ответ (время)


def test_bare_command_echo():
    """#316 R4/R5: филлеры (согласие/вежливость/дискурс) + пунктуация ОТОВСЮДУ, остался один write-глагол
    без объекта = эхо. R5 (оба Codex): хвостовые «удали пожалуйста»/«удали,» тоже эхо."""
    from sreda.runtime.react_signals import bare_command_echo as e
    for t in ("удали", "удалите", "ок удали", "ну давай удали", "да удали", "отмени", "  удали!",
              "удали пожалуйста", "ок удали пожалуйста", "удали,", "удали!!!", "ну давай ок удали",
              "тогда удали", "удали же", "УДАЛИ.", "ок. удали"):
        assert e(t), t
    # объект после глагола / read-глагол / местоимение-объект (residual) / пусто → НЕ эхо
    for t in ("удали задачу B", "удали покупки", "удали всё", "удали это", "покажи покупки",
              "добавь молоко", "ок", "давай", "да", "какие у меня дела", ""):
        assert not e(t), t


def test_withdrawal_messages():
    """#316 R2/R3: закрытие сироты — по одному withdrawal-ToolMessage на повисший tool_call, с
    result_kind=withdrawn (в метрике НЕ «ok»/исполнен). Не-AIMessage / без tool_calls → пусто."""
    f = react_loop._withdrawal_messages
    assert f(None) == []
    assert f(HumanMessage(content="hi")) == []
    assert f(AIMessage(content="plain")) == []  # AIMessage без tool_calls → сироты нет
    ai = AIMessage(content="", tool_calls=[
        {"name": "ask_human", "args": {}, "id": "c1"},
        {"name": "delete_task", "args": {"id": 5}, "id": "c2"}])
    out = f(ai)
    assert len(out) == 2 and all(isinstance(m, ToolMessage) for m in out)
    assert {m.tool_call_id for m in out} == {"c1", "c2"}
    assert all((m.artifact or {}).get("result_kind") == "withdrawn" for m in out)
    assert all("не считать выполненным" in str(m.content) for m in out)


def test_confirm_declined(install):
    """#321: гейт «детерминированный честный отказ» — confirm-пауза И resume НЕ «да» И канареечный
    тенант. Реальная функция, что зовёт handle_turn (тест бьёт код, не реимплементацию — спец-дрейф #74).
    Гейт _unified_execute_for → легаси НЕ трогаем (kill-switch)."""
    install()  # флаг ON, unified_tenants="t"
    f = react_loop._confirm_declined
    assert f(True, "нет", "t")          # канарейка + confirm-отказ (текст «нет»/«удали»/«ок» → канон «нет»; кнопка «Нет»)
    assert not f(True, "да", "t")       # confirm-подтверждение → success-путь НЕ трогаем
    assert not f(False, "нет", "t")     # ask_human (не confirm) → как есть
    assert not f(True, "нет", "other")  # НЕ канареечный тенант → легаси не трогаем


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
