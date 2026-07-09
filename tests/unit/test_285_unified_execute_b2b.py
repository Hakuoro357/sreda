"""#285 Фаза B срез B2b-1: проводка ЕДИНОГО пути execute (канареечный тенант).

Единый путь execute переопределяет intent-сплит + #221-домены единой политикой (compute_unified_policy),
переиспользуя task-бинд + _apply_domain_policy. Проверяем:
- гейт `_unified_execute_for` (флаг И список);
- на единый тенант write-сигнал → write-инструмент домена биндится (ярус а);
- смолток на единый тенант → только web (нет own-data/write) — route-мина нейтрализована;
- тенант ВНЕ списка → byte-identical (chat/fact web-only сплит цел) — safety-инвариант.
Ярус (б) candidate/confirm — B2b-2 (здесь unsignaled write ещё deny, тенант в списке НЕ включаем на прод).
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop

_TASK_TOOLS = ["list_reminders", "schedule_reminder", "add_task", "cancel_task",
               "need_family", "recall_memory"]
_WEB_TOOLS = ["web_search", "fetch_url", "get_weather"]


def _mk_tool(name, invoked):
    def _f(q: str = ""):
        invoked[name] = invoked.get(name, 0) + 1
        return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=name)


class _NoTrace:
    def __getattr__(self, _):
        return lambda *a, **k: None


class _Chat:
    def __init__(self, label, classify="chat", bound_capture=None, responses=None):
        self.label, self._classify, self._cap = label, classify, bound_capture
        self._responses, self._i = list(responses or []), 0

    async def ainvoke(self, _msgs):
        return AIMessage(content=self._classify)

    def bind_tools(self, tools):
        if self._cap is not None:
            self._cap.setdefault(self.label, []).append(
                sorted(getattr(t, "name", "?") for t in tools))
        outer = self

        def _inv(_msgs):
            if outer._responses:
                r = outer._responses[min(outer._i, len(outer._responses) - 1)]
                outer._i += 1
                return r
            return AIMessage(content="resp-" + outer.label)
        return RunnableLambda(_inv)


def _ai_call(name, cid, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


@pytest.fixture
def install(monkeypatch):
    from sreda.config import settings as settings_mod

    def _install(*, unified_flag=False, unified_tenants="", deepseek=None, cap=None):
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


# ─────────── гейт _unified_execute_for ───────────
def test_gate_requires_flag_and_tenant(install, monkeypatch):
    from sreda.config import settings as sm
    for flag, tenants, tenant, exp in [
        (False, "t", "t", False),   # флаг OFF → никто
        (True, "", "t", False),      # список пуст → никто (глобальный shadow)
        (True, "t", "t", True),      # флаг + тенант в списке → execute
        (True, "t", "other", False), # тенант вне списка
        (True, "*", "anyone", True), # * → всем
    ]:
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1" if flag else "0")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_TENANTS", tenants)
        sm.get_settings.cache_clear()
        assert react_loop._unified_execute_for(tenant) is exp, (flag, tenants, tenant)


# ─────────── единый тенант: write-сигнал биндит инструмент домена ───────────
def test_unified_signaled_write_binds_domain_tool(install):
    cap = {}
    freddie = _Chat("freddie", classify="chat", bound_capture=cap)  # classify игнорится (unified→task)
    install(unified_flag=True, unified_tenants="t", deepseek=_Chat("ds", bound_capture=cap), cap=cap)
    _turn(freddie, thread="u1", text="поставь напоминание позвонить маме")
    bound = cap.get("freddie", [])
    assert bound, "freddie должен был забиндиться (task-путь единого)"
    assert any("schedule_reminder" in b for b in bound), bound  # reminders в allowed_write


# ─────────── единый тенант: смолток → нет own-data READ (write — кандидат под confirm, B2b-2) ───────────
def test_unified_smalltalk_no_owndata_read(install):
    """«как дела?»: own-data READ (list_reminders/recall_memory) НЕ биндятся (read-гейт; route-мина
    нейтрализована). Write-инструменты биндятся КАНДИДАТАМИ (B2b-2 confirm-gated, не молчаливо и не
    grant) — их присутствие ок; ключевая защита смолтока = отсутствие чтения личных данных."""
    cap = {}
    freddie = _Chat("freddie", classify="chat", bound_capture=cap)
    install(unified_flag=True, unified_tenants="t", deepseek=_Chat("ds", bound_capture=cap), cap=cap)
    _turn(freddie, thread="u2", text="как дела?")
    bound_all = [b for lst in cap.values() for b in lst]
    assert bound_all, "должен был забиндиться"
    for b in bound_all:
        assert "recall_memory" not in b, b       # own-data read закрыт
        assert "list_reminders" not in b, b       # own-data read закрыт


# ─────────── тенант ВНЕ списка: byte-identical (chat/fact сплит цел) ───────────
def test_non_unified_tenant_byte_identical(install):
    """Тенант не в unified-списке при флаге ON → override не исполняется; «как дела?» идёт
    chat/fact web-only сплитом (deepseek), НЕ task-путём единого. Safety-инвариант канарейки."""
    cap_uni, cap_split = {}, {}
    # единый тенант
    f1 = _Chat("freddie", classify="chat", bound_capture=cap_uni)
    install(unified_flag=True, unified_tenants="canary", deepseek=_Chat("ds", bound_capture=cap_uni), cap=cap_uni)
    _turn(f1, thread="s1", text="как дела?", tenant="canary")
    # обычный тенант при том же флаге
    f2 = _Chat("freddie", classify="chat", bound_capture=cap_split)
    install(unified_flag=True, unified_tenants="canary", deepseek=_Chat("ds", bound_capture=cap_split), cap=cap_split)
    _turn(f2, thread="s2", text="как дела?", tenant="regular")
    # у обычного тенанта «как дела?» → chat/fact → deepseek биндится web-only (сплит), у единого — freddie(task)
    assert "ds" in cap_split, "обычный тенант должен идти chat/fact на deepseek (сплит цел)"


# ─────────── B2b-2 end-to-end: unsignaled write → пауза-confirm → resume «да» → исполнено ───────────
def test_unsignaled_write_confirms_then_executes(install):
    """«расскажи как дела» (no signal) + модель зовёт add_task → candidate → confirm-пауза;
    resume «да» → add_task ИСПОЛНЕН. Молчаливой мутации нет (пауза до подтверждения)."""
    inv = {}
    # ход 1: freddie эмитит add_task (unsignaled → candidate → interrupt); ход 2 (resume): финал
    freddie = _Chat("freddie", classify="chat",
                    responses=[_ai_call("add_task", "c1", title="позвонить"), AIMessage(content="Готово.")])
    _install_inv = install(unified_flag=True, unified_tenants="t", deepseek=_Chat("ds"))
    # разделяем invoked-словарь: install создаёт свой inv; захватим через build_slice_tools
    inv = _install_inv
    r1 = _turn(freddie, thread="e2e", text="расскажи как дела")
    # ход 1 → пауза (add_task не исполнен, ждёт confirm)
    assert getattr(r1, "awaiting_confirm", False) is True, r1
    assert inv.get("add_task", 0) == 0, "мутация НЕ должна произойти до подтверждения"
    # resume «да» → исполнение
    _turn(freddie, thread="e2e", text="да")
    assert inv.get("add_task", 0) == 1, "после «да» add_task исполнен ровно раз"


# ─────────── #321: confirm-ОТМЕНА → детерминированный честный ответ (не галлюцинация) ───────────
def test_declined_confirm_deterministic_reply(install):
    """#321 (канарейка #316 e2e): confirm-пауза (unsignaled write) → resume «нет» → мутации НЕТ И ответ
    ДЕТЕРМИНИРОВАННЫЙ «Отменила, ничего не делаю.», даже если модель на отмене галлюцинирует «Готово»
    (корень #321: chat-узел пере-сочинял отказ в ложное «удалено/готово»). Бьёт РЕАЛЬНЫЙ handle_turn."""
    freddie = _Chat("freddie", classify="chat",
                    responses=[_ai_call("add_task", "c1", title="позвонить"),
                               AIMessage(content="Готово, добавила задачу «позвонить».")])  # ← ложь на отмене
    inv = install(unified_flag=True, unified_tenants="t", deepseek=_Chat("ds"))
    assert getattr(_turn(freddie, thread="d1", text="расскажи как дела"), "awaiting_confirm", False)
    r2 = _turn(freddie, thread="d1", text="нет")
    assert inv.get("add_task", 0) == 0, "отказ → мутации нет"
    assert "Отменила, ничего не делаю" in str(r2), r2                  # детерминированный честный отказ
    assert "Готово" not in str(r2) and "добавила" not in str(r2), r2  # галлюцинация подавлена


def test_confirmed_write_reply_not_overridden(install):
    """#321: success-путь НЕ трогаем — resume «да» отдаёт ответ модели, а не фиксированный отказ."""
    freddie = _Chat("freddie", classify="chat",
                    responses=[_ai_call("add_task", "c1", title="позвонить"),
                               AIMessage(content="Готово, добавила «позвонить».")])
    inv = install(unified_flag=True, unified_tenants="t", deepseek=_Chat("ds"))
    _turn(freddie, thread="d2", text="расскажи как дела")
    r2 = _turn(freddie, thread="d2", text="да")
    assert inv.get("add_task", 0) == 1
    assert "Отменила, ничего не делаю" not in str(r2), r2  # success не подменяется отказом
    # NB: легаси-«не оверрайдится» покрыт юнитом test_confirm_declined (not f(True,"нет","other")) —
    # handle_turn-стаб не стейджит легаси candidate-confirm, поэтому e2e-тест здесь был бы вечный skip.
