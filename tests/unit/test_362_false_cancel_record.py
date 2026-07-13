"""#362 — Ложная отмена на записи данных (регрессия честной-отмены #321 на НЕ-отменяющем сообщении).

Прод-инцидент (пользователь ведёт журнал показателей): декларативная запись показателя на ЖИВОЙ
confirm-паузе единого пути (#285) детерминированно превращалась в «Отменила, ничего не делаю.» +
показатель ПОТЕРЯН. Причина: `_should_redirect_on_pause` (confirm-ветка) требовал КОМАНДНЫЙ сигнал
(`_is_new_request_on_pause`), а голая ДЕКЛАРАТИВНАЯ запись («Сахар утром 16 и 2») его не несёт →
проваливалась в resume→«нет»→`_confirm_declined`→ложная отмена (показатель дискардился: resume
подменяет user_text на «нет», модель его больше не видит). Идентификаторы/детали инцидента — в
закрытом issue, не в этом (публичном) репозитории.

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


class _ChatRecord:
    """Input-aware Фредди: эмитит add_task(показатель) РОВНО ОДИН раз, когда видит МАРКЕР показателя в
    ПОСЛЕДНЕЙ human-реплике. Моделирует реальность: current resume-путь подменяет user_text на «нет»
    (модель показатель НЕ видит → не пишет); fixed redirect-путь отдаёт показатель свежим HumanMessage
    (модель видит → пишет). Иначе (нет показателя / уже записан) — финальный ответ."""

    def __init__(self, first, record=SUGAR, marker="ахар"):
        self.first, self.record, self.marker = first, record, marker
        self.n, self.recorded = 0, False

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
            if outer.marker in last_h and not outer.recorded:
                outer.recorded = True
                return AIMessage(content="", tool_calls=[
                    {"name": "add_task", "args": {"title": outer.record}, "id": f"s{outer.n}"}])
            return AIMessage(content="Записала показатель.")
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
def test_confirm_reply_is_noise():
    """#362 R3 (протокол-семантика): на confirm-паузе НЕ-содержательная реплика (эхо/filler/чистый отказ)
    → noise=True (resume/fail-closed); СОДЕРЖАТЕЛЬНАЯ (запись любой формы/новый запрос/dual-intent) → False."""
    from sreda.runtime.react_signals import confirm_reply_is_noise as n
    # noise=True — эхо-подтверждение / filler (→ fail-closed #316/#267)
    for t in ("", "ок", "окей", "конечно", "давай", "ну давай", "удали", "удалите", "ок удали",
              "давай удаляй", "удаляй", "ок удаляй", "да удали", "удали пожалуйста"):
        assert n(t), f"{t!r} — эхо/filler, должно быть noise (не редирект)"
    # noise=True — ЧИСТЫЙ отказ (ведущее отрицание + пустой/числовой/id-хвост, цифрой И словом)
    for t in ("нет, 16", "нет 16", "отмена, задача 5", "не надо 16", "отмена, задача пять",
              "нет, шестнадцать"):
        assert n(t), f"{t!r} — чистый отказ, должно быть noise (честная отмена #321)"
    # noise=False — СОДЕРЖАТЕЛЬНОЕ: запись цифрой/словом/КАЧЕСТВЕННАЯ, новый запрос, dual-intent
    for t in (SUGAR, "сахар 16 и 2", "16.2", "давление сто двадцать на восемьдесят",
              "температура высокая", "сахар низкий", "самочувствие плохое",
              "покажи покупки", "добавь молоко в покупки", "нет, сахар 16"):
        assert not n(t), f"{t!r} — содержательное, должно РЕДИРЕКТИТЬ (не noise)"


# ─────────── юнит: _should_redirect_on_pause (confirm-ветка) ───────────
def test_should_redirect_declarative_record_362():
    """#362: декларативная запись показателя на confirm-паузе → redirect (свежий ход), НЕ ложная отмена.
    #321 сохранён: «нет»/«отмена» → НЕ redirect (честная отмена). #316: голое «удали» → НЕ redirect."""
    f = react_loop._should_redirect_on_pause
    # запись показателя на confirm → redirect (было False → ложная «Отменила»); цифрами, словами,
    # КАЧЕСТВЕННАЯ (R3, оба Codex MAJOR — «температура высокая» без числа тоже терялась), dual-intent
    for t in (SUGAR, "сахар 16 и 2", "16.2", "давление 120 на 80",
              "сахар шестнадцать и два", "вес восемьдесят",
              "температура высокая", "сахар низкий", "нет, сахар 16"):
        assert f(t, is_confirm_pause=True), f"confirm: запись {t!r} должна redirect (свежий ход)"
    # честная отмена #321 — НЕ redirect (уходит в resume→honest «Отменила»); в т.ч. с числовым хвостом
    for t in ("нет", "отмена", "не надо", "нет, 16", "отмена, задача 5", "нет, шестнадцать"):
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


# ─────────── e2e: ложная отмена убрана + показатель дошёл до записи ───────────
@pytest.mark.parametrize("record,marker", [
    (SUGAR, "ахар"),                 # числовая запись (прод-инцидент)
    ("температура высокая", "емператур"),  # R3 (оба Codex MAJOR): КАЧЕСТВЕННАЯ запись без числа
])
def test_e2e_declarative_record_not_falsely_cancelled_362(install, monkeypatch, tmp_path, record, marker):
    """#362 e2e (чеклист issue), ДВА ассерта:
    (а) декларативная запись показателя на живой confirm-паузе → ответ НЕ «Отменила» (свежий ход);
    (б) СОСТОЯНИЕ ДАННЫХ — показатель дошёл до записи: инструмент add_task ВЫЗВАН с payload показателя
        (детерминированная идемпотентная запись живёт в сервисе; здесь бьём диспатч+payload+confirm-барьер,
        не storage-слой).

    Без фикса: R2 → «Отменила, ничего не делаю.» + inv пуст (показатель дискарден: resume подменяет
    user_text на «нет», модель показатель не видит) → оба ассерта КРАСНЫЕ."""
    saver = _durable_saver(tmp_path, f"ck362e-{marker}.db")
    monkeypatch.setattr(react_loop, "_persist_enabled", lambda: True)
    monkeypatch.setattr(react_loop, "_get_checkpointer", lambda: saver)
    inv = install()
    thread = f"e362{marker}"
    freddie = _ChatRecord(_ai_call("add_task", "c1", title="проверка"), record=record, marker=marker)

    r1 = _turn(freddie, thread=thread, text=UNSIGNALLED)
    assert getattr(r1, "awaiting_confirm", False) is True, r1  # живая пауза P1

    r2 = _turn(freddie, thread=thread, text=record)
    # (а) НЕ ложная отмена
    assert OTMENILA not in str(r2), f"запись показателя НЕ должна давать «Отменила»: {r2!r}"
    # свежий ход поднял НОВУЮ candidate-паузу под показатель (universal confirm; НЕ молчаливая запись)
    assert getattr(r2, "awaiting_confirm", False) is True, f"ожидали свежий confirm под показатель: {r2!r}"
    assert inv.get("add_task", 0) == 0, "до подтверждения запись НЕ исполняется (нет молчаливой мутации)"

    # (б) показатель дошёл до записи: подтверждаем свежую паузу → add_task ВЫЗВАН с payload показателя
    r3 = _turn(freddie, thread=thread, text="да")
    assert inv.get("add_task", 0) == 1, "показатель должен дойти до записи РОВНО раз (add_task вызван), не потерян"
    assert inv.get("add_task_title") == record, \
        f"инструмент вызван именно с показателем: {inv.get('add_task_title')!r}"
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


@pytest.mark.parametrize("refusal", ["нет, 16", "отмена, задача 5", "не надо"])
def test_genuine_decline_with_number_tail_still_honest_362(install, monkeypatch, tmp_path, refusal):
    """#362 R2 (оба Codex MAJOR): ведущее ОТРИЦАНИЕ с числовым/пунктуационным хвостом («нет, 16») —
    это отказ, а НЕ запись показателя → честная детерминированная «Отменила», мутация НЕ исполнена.
    Иначе data_record_signal='любое число' съедал бы подтверждённый отказ (регрессия #321)."""
    saver = _durable_saver(tmp_path, f"ck362n-{abs(hash(refusal))}.db")
    monkeypatch.setattr(react_loop, "_persist_enabled", lambda: True)
    monkeypatch.setattr(react_loop, "_get_checkpointer", lambda: saver)
    inv = install()
    freddie = _Chat("freddie", responses=[
        _ai_call("add_task", "c1", title="проверка"),
        AIMessage(content="галлюцинация про успех"),
        AIMessage(content="fall"),
    ])
    r1 = _turn(freddie, thread=f"n362{abs(hash(refusal))}", text=UNSIGNALLED)
    assert getattr(r1, "awaiting_confirm", False) is True, r1
    r2 = _turn(freddie, thread=f"n362{abs(hash(refusal))}", text=refusal)
    assert OTMENILA in str(r2), f"отказ {refusal!r} → честная отмена: {r2!r}"
    assert inv.get("add_task", 0) == 0, "отказ → мутация НЕ исполнена"


def test_antiparrot_clause_in_withdrawal_persists_362(install, monkeypatch, tmp_path):
    """#362 R3 (Codex sol+terra MAJOR): анти-паррот-клауза ЖИВЁТ В САМОМ withdrawal-ToolMessage → доходит
    до модели на редиректе И переживает ВСЕ проходы хода (в отличие от consume-and-clear директивы). Без
    неё «вызов инструмента отменён» + availability-хвост провоцируют паррот «Отменила» (2-й прод-симптом).
    Проверяем доставку и на pass-1 (с показателем), и на pass-2 (после промежуточного tool-call)."""
    saver = _durable_saver(tmp_path, "ck362rn.db")
    monkeypatch.setattr(react_loop, "_persist_enabled", lambda: True)
    monkeypatch.setattr(react_loop, "_get_checkpointer", lambda: saver)
    captured: list = []

    class _Cap:
        def __init__(self, first):
            self.first, self.n = first, 0

        async def ainvoke(self, _m):
            return AIMessage(content="chat")

        def bind_tools(self, tools):
            outer = self

            def _inv(msgs):
                outer.n += 1
                captured.append(list(msgs))
                if outer.n == 1:
                    return outer.first          # turn1 → candidate-confirm
                if outer.n == 2:                 # redirect pass-1 → промежуточный tool-call
                    return _ai_call("list_reminders", "l1")
                return AIMessage(content="Готово.")  # redirect pass-2 → финал
            return RunnableLambda(_inv)

    install()
    freddie = _Cap(_ai_call("add_task", "c1", title="проверка"))
    r1 = _turn(freddie, thread="rn362", text=UNSIGNALLED)
    assert getattr(r1, "awaiting_confirm", False) is True, r1
    n_before = len(captured)
    _turn(freddie, thread="rn362", text=SUGAR)  # redirect → свежий ход (2 прохода модели)
    fresh_inputs = captured[n_before:]
    assert len(fresh_inputs) >= 2, "ожидали ≥2 прохода модели на редиректе (иначе persist-проверка пуста)"
    flat_all = _flat_msgs(fresh_inputs)
    assert "вызов инструмента отменён" in flat_all, "премиса: withdrawal-ToolMessage в истории свежего хода"
    # клауза ПЕРСИСТИТ: доходит на КАЖДОМ проходе (withdrawal остаётся в истории, не consume-and-clear)
    for i, cap in enumerate(fresh_inputs[:2]):
        assert "НЕ сообщать об этом как об отмене" in _flat_msgs([cap]), \
            f"анти-паррот-клауза должна дойти до модели на проходе {i + 1} (persist)"


def _flat_msgs(caps):
    out = []
    for cap in caps:
        for m in cap:
            out.append(str(getattr(m, "content", "")))
    return "\n".join(out)
