"""#362 — Ложная отмена на записи данных (регрессия честной-отмены #321 на НЕ-отменяющем сообщении).

Прод (MAX-юзер tenant_max_142322319, диабетик): декларативная запись показателя на ЖИВОЙ
confirm-паузе единого пути (#285) детерминированно превращалась в «Отменила, ничего не делаю.» +
показатель ПОТЕРЯН. Причина: `_should_redirect_on_pause` (confirm-ветка) требовал КОМАНДНЫЙ сигнал
(`_is_new_request_on_pause`), а голая ДЕКЛАРАТИВНАЯ запись («Сахар утром 16 и 2») его не несёт →
проваливалась в resume→«нет»→`_confirm_declined`→ложная отмена (показатель дискардился: resume
подменяет user_text на «нет», модель его больше не видит).

Фикс (механизм): `react_signals.data_record_signal` (число-измерение) добавлен в confirm-ветку
`_should_redirect_on_pause` — у confirm-паузы нет свободных СЛОТ-ответов, поэтому содержательная
НЕ-да/нет реплика с числом = свежий ход, а не отказ. ask_human-ветка НЕ тронута («865»/«в 18:00» —
валидные ОТВЕТЫ). Честная отмена #321 («нет»/«отмена» → classify=negate) сохранена.

Канал-агностично: гейт `_unified_execute_for(tenant)` (unified=`*` на всех) — бьёт и MAX, и TG.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop

# Явные Cyrillic-константы — прод-формулировки показателя.
SUGAR = "Сахар утром 16 и 2"
OTMENILA = "Отменила"
UNSIGNALLED = "расскажи как дела"  # нет write-сигнала → add_task модели становится candidate-confirm

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


def _clean_add_task(inv):
    """Чистая сигнатура (без **kw): на resume-re-execute candidate-а поле kw=None ломает ре-валидацию
    (см. #325 test_e2e_325). Нужна, чтобы «да» реально ИСПОЛНИЛ запись → ассерт на состояние данных."""
    def _f(title: str = "", q: str = "") -> str:
        inv["add_task"] = inv.get("add_task", 0) + 1
        inv["add_task_title"] = title or q
        return "add_task-ok"
    return StructuredTool.from_function(func=_f, name="add_task", description="add a task")


class _NoTrace:
    def __getattr__(self, _):
        return lambda *a, **k: None


class _Chat:
    def __init__(self, label, *, responses=None):
        self.label = label
        self._responses, self._i = list(responses or []), 0

    async def ainvoke(self, _msgs):
        return AIMessage(content="chat")

    def bind_tools(self, tools):
        outer = self

        def _inv(_msgs):
            if outer._responses:
                r = outer._responses[min(outer._i, len(outer._responses) - 1)]
                outer._i += 1
                return r
            return AIMessage(content="resp-" + outer.label)
        return RunnableLambda(_inv)


class _ChatSugar:
    """Input-aware Фредди: эмитит add_task(показатель) РОВНО ОДИН раз, когда видит показатель в
    ПОСЛЕДНЕЙ human-реплике. Моделирует реальность: current resume-путь подменяет user_text на «нет»
    (модель показатель НЕ видит → не пишет); fixed redirect-путь отдаёт показатель свежим HumanMessage
    (модель видит → пишет). Иначе (нет показателя / уже записан) — финальный ответ."""

    def __init__(self, first):
        self.first, self.n, self.recorded = first, 0, False

    async def ainvoke(self, _msgs):
        return AIMessage(content="chat")

    def bind_tools(self, tools):
        outer = self

        def _inv(msgs):
            outer.n += 1
            last_h = ""
            for mm in reversed(list(msgs)):
                if isinstance(mm, HumanMessage):
                    last_h = str(getattr(mm, "content", ""))
                    break
            if outer.n == 1 and outer.first is not None:
                return outer.first
            if "ахар" in last_h and not outer.recorded:
                outer.recorded = True
                return AIMessage(content="", tool_calls=[
                    {"name": "add_task", "args": {"title": SUGAR}, "id": f"s{outer.n}"}])
            return AIMessage(content="Записала показатель сахара.")
        return RunnableLambda(_inv)


@pytest.fixture
def install(monkeypatch):
    from sreda.config import settings as settings_mod

    def _install(*, tenant="t"):
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_TENANTS", tenant)
        settings_mod.get_settings.cache_clear()
        inv = {}
        monkeypatch.setattr(react_loop, "build_slice_tools", lambda *a, **k: [
            _mk_tool(n, inv) for n in (_TASK_TOOLS + _WEB_TOOLS) if n != "add_task"]
            + [_clean_add_task(inv)])
        monkeypatch.setattr(react_loop, "_trace", _NoTrace())
        monkeypatch.setattr(react_loop, "_record_react_usage", lambda **k: None)
        import sreda.services.llm as llm_mod
        monkeypatch.setattr(llm_mod, "get_chat_llm", lambda *a, **k: _Chat("ds"))
        return inv

    yield _install
    settings_mod.get_settings.cache_clear()


def _turn(freddie, *, thread, text, tenant="t"):
    return asyncio.run(react_loop.handle_turn(
        session=None, tenant_id=tenant, user_id="u", thread_id=thread,
        llm=freddie, user_text=text, inbound_message_id=f"{thread}:{text[:10]}",
        channel="react", resume_only=False, expected_confirm_id="",
        provider_key="inception-mercury2", fallback_llm=None))


def _durable_saver(tmp_path, name):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from sreda.db.base import Base
    from sreda.runtime.react_checkpoint_saver import EncryptedSqlCheckpointSaver
    engine = create_engine(f"sqlite:///{tmp_path / name}",
                           connect_args={"check_same_thread": False}, poolclass=NullPool)
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return EncryptedSqlCheckpointSaver(session_factory=SF)


# ─────────── юнит: data_record_signal ───────────
def test_data_record_signal():
    """#362: число-измерение = знак декларативной записи показателя; да/нет/эхо/без числа → False."""
    from sreda.runtime.react_signals import data_record_signal as f
    for t in (SUGAR, "сахар 16 и 2", "16.2", "давление 120 на 80", "вес 80", "пульс 72"):
        assert f(t), f"{t!r} должен быть записью показателя"
    for t in ("сахар", "удали", "покажи покупки", "да", "нет", "отмена", "", "какой у меня вес"):
        assert not f(t), f"{t!r} НЕ должен считаться записью показателя"


# ─────────── юнит: _should_redirect_on_pause (confirm-ветка) ───────────
def test_should_redirect_declarative_record_362():
    """#362: декларативная запись показателя на confirm-паузе → redirect (свежий ход), НЕ ложная отмена.
    #321 сохранён: «нет»/«отмена» → НЕ redirect (честная отмена). #316: голое «удали» → НЕ redirect."""
    f = react_loop._should_redirect_on_pause
    # запись показателя на confirm → redirect (было False → ложная «Отменила»)
    for t in (SUGAR, "сахар 16 и 2", "16.2", "давление 120 на 80"):
        assert f(t, is_confirm_pause=True), f"confirm: запись {t!r} должна redirect (свежий ход)"
    # честная отмена #321 — НЕ redirect (уходит в resume→honest «Отменила»)
    for t in ("нет", "отмена", "не надо"):
        assert not f(t, is_confirm_pause=True), f"confirm: {t!r} — честная отмена, НЕ redirect"
    # аффирматив — НЕ redirect (штатный resume «да»)
    for t in ("да", "ага", "подтверждаю"):
        assert not f(t, is_confirm_pause=True), t
    # голое эхо-подтверждение удаления (#316) — НЕ redirect (fail-closed)
    for t in ("удали", "ок удали", "удаляй"):
        assert not f(t, is_confirm_pause=True), t
    # ask_human-ветка НЕ тронута: число-ОТВЕТ на уточнение НЕ redirect (иначе бросили бы валидный ответ)
    for t in ("865", "в 18:00", "16.2"):
        assert not f(t, is_confirm_pause=False), f"ask_human: {t!r} — валидный ОТВЕТ, НЕ redirect"


# ─────────── e2e: ложная отмена убрана + показатель записан (ассерт на состояние данных) ───────────
def test_e2e_declarative_record_not_falsely_cancelled_362(install, monkeypatch, tmp_path):
    """#362 e2e (чеклист issue), ДВА ассерта:
    (а) «Сахар утром 16 и 2» на живой confirm-паузе → ответ НЕ «Отменила» (свежий ход, не ложная отмена);
    (б) АССЕРТ НА СОСТОЯНИЕ ДАННЫХ — показатель РЕАЛЬНО записан (add_task исполнен с текстом показателя).

    Без фикса: R2 → «Отменила, ничего не делаю.» + inv пуст (показатель дискарден: resume подменяет
    user_text на «нет», модель показатель не видит) → оба ассерта КРАСНЫЕ."""
    saver = _durable_saver(tmp_path, "ck362e.db")
    monkeypatch.setattr(react_loop, "_persist_enabled", lambda: True)
    monkeypatch.setattr(react_loop, "_get_checkpointer", lambda: saver)
    inv = install()
    freddie = _ChatSugar(_ai_call("add_task", "c1", title="проверка"))  # turn1 → candidate-confirm P1

    r1 = _turn(freddie, thread="e362", text=UNSIGNALLED)
    assert getattr(r1, "awaiting_confirm", False) is True, r1  # живая пауза P1

    r2 = _turn(freddie, thread="e362", text=SUGAR)
    # (а) НЕ ложная отмена
    assert OTMENILA not in str(r2), f"запись показателя НЕ должна давать «Отменила»: {r2!r}"

    # (б) показатель дошёл до записи: подтверждаем свежую паузу → add_task ИСПОЛНЕН с текстом показателя
    r3 = _turn(freddie, thread="e362", text="да")
    assert inv.get("add_task", 0) >= 1, "показатель должен быть ЗАПИСАН (add_task исполнен), не потерян"
    assert inv.get("add_task_title") == SUGAR, \
        f"записан именно показатель: {inv.get('add_task_title')!r}"
    assert OTMENILA not in str(r3), r3


# ─────────── регрессия #321: настоящая отмена по-прежнему честная ───────────
def test_genuine_decline_still_honest_321(install, monkeypatch, tmp_path):
    """#321 НЕ сломан: «нет»/«отмена» на настоящей confirm-паузе → честная детерминированная «Отменила»
    + мутация НЕ исполнена (candidate declined)."""
    saver = _durable_saver(tmp_path, "ck362d.db")
    monkeypatch.setattr(react_loop, "_persist_enabled", lambda: True)
    monkeypatch.setattr(react_loop, "_get_checkpointer", lambda: saver)
    inv = install()
    freddie = _Chat("freddie", responses=[
        _ai_call("add_task", "c1", title="проверка"),   # turn1 → candidate-confirm P1
        AIMessage(content="галлюцинация про успех"),      # post-resume финал (заменяется #321-текстом)
        AIMessage(content="fall"),
    ])
    r1 = _turn(freddie, thread="d321", text=UNSIGNALLED)
    assert getattr(r1, "awaiting_confirm", False) is True, r1
    r2 = _turn(freddie, thread="d321", text="нет")
    assert OTMENILA in str(r2), f"честная отмена #321 должна остаться: {r2!r}"
    assert inv.get("add_task", 0) == 0, "отказ → мутация НЕ исполнена"
