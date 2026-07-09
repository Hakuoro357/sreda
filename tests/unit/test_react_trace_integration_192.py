"""#192 Фаза B/C — интеграция: handle_turn при ВКЛ флаге пишет react_turn_trace (e2e).

Топ-1 (полнота) + топ-2 (in_progress при потерянном finish) чеклиста приёмки.
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from sreda.config import settings as st_mod
from sreda.db.models import ReactTurnTrace
from sreda.runtime import react_loop, react_trace_persist
from tests.unit.conftest import seed_telegram_user
from tests.unit.test_react_lazy_families_165 import _RecordingStubLLM


def _u(p: int, c: int) -> dict:
    return {"input_tokens": p, "output_tokens": c, "total_tokens": p + c}


@pytest.fixture()
def trace_on(tmp_path, monkeypatch):
    # ОТДЕЛЬНЫЙ file-engine для трейса (развязка от shared :memory: db_session: SF на shared engine
    # делит StaticPool-соединение → commit трейса коммитил бы транзакцию db_session, ломая изоляцию).
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base

    monkeypatch.setenv("SREDA_REACT_TRACE_ENABLED", "1")
    st_mod.get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{tmp_path / 'trace.db'}")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine)
    monkeypatch.setattr(react_trace_persist, "_session", lambda: SF())
    yield SF
    st_mod.get_settings.cache_clear()


def _trace_rows(SF, tenant_id):
    s = SF()
    try:
        return s.query(ReactTurnTrace).filter(ReactTurnTrace.tenant_id == tenant_id).all()
    finally:
        s.close()


@pytest.mark.asyncio
async def test_trace_integration_fresh_turn_done(db_session, trace_on):
    """Топ-1: свежий ход при ВКЛ флаге → ОДНА строка done со структурой (llm_calls, outcome, контент)."""
    SF = trace_on
    u = seed_telegram_user(db_session)
    db_session.flush()
    stub = _RecordingStubLLM([AIMessage(content="Готово.", usage_metadata=_u(10, 5))])

    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id="tr-done",
        llm=stub, user_text="привет, как дела", inbound_message_id="m-done",
        channel="telegram", provider_key="inception-mercury2")
    assert "Готово" in str(r)

    rows = _trace_rows(SF, u.tenant_id)
    assert len(rows) == 1, f"ожидали 1 строку трейса, получили {len(rows)}"
    row = rows[0]
    assert row.status == "done" and row.outcome == "ok"
    assert row.origin_user_text == "привет, как дела"  # ORM расшифровывает
    assert row.finished_at is not None
    llm = json.loads(row.llm_calls_json or "[]")
    assert len(llm) >= 1 and llm[0]["provider_key"] == "inception-mercury2"
    assert "latency_ms" in llm[0]


@pytest.mark.asyncio
async def test_trace_integration_in_progress_when_finish_lost(db_session, trace_on, monkeypatch):
    """Топ-2: finish-хук «потерян» (краш/таймаут смоделирован no-op finish) → строка in_progress."""
    SF = trace_on
    monkeypatch.setattr(react_trace_persist, "persist_trace_finish", lambda **kw: None)
    u = seed_telegram_user(db_session)
    db_session.flush()
    stub = _RecordingStubLLM([AIMessage(content="Готово.", usage_metadata=_u(10, 5))])

    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id="tr-inp",
        llm=stub, user_text="привет", inbound_message_id="m-inp",
        channel="telegram", provider_key="inception-mercury2")

    rows = _trace_rows(SF, u.tenant_id)
    assert len(rows) == 1 and rows[0].status == "in_progress"  # старт есть, финал не дошёл
    assert rows[0].finished_at is None


@pytest.mark.asyncio
async def test_trace_integration_disabled_no_row(db_session, _test_engine, monkeypatch):
    """Флаг ВЫКЛ → строка трейса НЕ пишется (откат к #185)."""
    from sqlalchemy.orm import sessionmaker

    monkeypatch.delenv("SREDA_REACT_TRACE_ENABLED", raising=False)
    st_mod.get_settings.cache_clear()
    SF = sessionmaker(bind=_test_engine)
    monkeypatch.setattr(react_trace_persist, "_session", lambda: SF())
    try:
        u = seed_telegram_user(db_session)
        db_session.flush()
        stub = _RecordingStubLLM([AIMessage(content="Готово.", usage_metadata=_u(10, 5))])
        await react_loop.handle_turn(
            session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id="tr-off",
            llm=stub, user_text="привет", inbound_message_id="m-off",
            channel="telegram", provider_key="inception-mercury2")
        assert _trace_rows(SF, u.tenant_id) == []
    finally:
        st_mod.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_trace_routing_decision_execute(db_session, trace_on, monkeypatch):
    """#221 Ф3b: в execute строка трейса несёт routing_decision_json (mode=execute + домен + allowed)."""
    SF = trace_on
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    u = seed_telegram_user(db_session)
    db_session.flush()
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", u.tenant_id)
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", u.tenant_id)  # Ф4: канареечный список
    st_mod.get_settings.cache_clear()
    stub = _RecordingStubLLM([AIMessage(content="Готово.", usage_metadata=_u(10, 5))])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id="tr-rd",
        llm=stub, user_text="напомни купить молоко", inbound_message_id="m-rd",
        channel="telegram", provider_key="inception-mercury2")
    rows = _trace_rows(SF, u.tenant_id)
    assert len(rows) == 1
    rd = json.loads(rows[0].routing_decision_json)
    assert rd["mode"] == "execute" and rd["primary_domain"] == "reminders"
    assert "reminders" in rd["allowed_read"] and rd["confidence"] == "deterministic"


@pytest.mark.asyncio
async def test_trace_routing_decision_disabled_null(db_session, trace_on, monkeypatch):
    """disabled (дефолт) → routing_decision_json NULL (никаких новых данных при выключенном роутере)."""
    SF = trace_on
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
    monkeypatch.delenv("SREDA_REACT_DOMAIN_SCOPE_MODE", raising=False)  # disabled
    u = seed_telegram_user(db_session)
    db_session.flush()
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", u.tenant_id)
    st_mod.get_settings.cache_clear()
    stub = _RecordingStubLLM([AIMessage(content="Готово.", usage_metadata=_u(10, 5))])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id="tr-rd0",
        llm=stub, user_text="напомни купить молоко", inbound_message_id="m-rd0",
        channel="telegram", provider_key="inception-mercury2")
    rows = _trace_rows(SF, u.tenant_id)
    assert len(rows) == 1 and rows[0].routing_decision_json is None


@pytest.mark.asyncio
async def test_trace_routing_decision_reset_no_stale(db_session, trace_on, monkeypatch):
    """#221 Ф3b-фикс: router_decision_json СБРАСЫВАЕТСЯ каждый свежий ход. Ход, пропустивший доменный блок
    (здесь: 2-й ход того же треда при mode=disabled), пишет в трейс None, а НЕ стейл-решение прошлого хода."""
    SF = trace_on
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
    u = seed_telegram_user(db_session)
    db_session.flush()
    monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", u.tenant_id)
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", u.tenant_id)
    stub = _RecordingStubLLM([AIMessage(content="ок", usage_metadata=_u(10, 5)),
                              AIMessage(content="ок2", usage_metadata=_u(10, 5))])
    # ход 1: execute → router_decision_json = решение (reminders) в чекпойнте треда
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
    st_mod.get_settings.cache_clear()
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id="reset-th",
        llm=stub, user_text="напомни купить молоко", inbound_message_id="m-r1",
        channel="telegram", provider_key="inception-mercury2")
    # ход 2: ТОТ ЖЕ тред, mode=disabled → доменный блок пропущен → решение должно быть None (сброс)
    monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "disabled")
    st_mod.get_settings.cache_clear()
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id="reset-th",
        llm=stub, user_text="привет", inbound_message_id="m-r2",
        channel="telegram", provider_key="inception-mercury2")
    rows = sorted(_trace_rows(SF, u.tenant_id), key=lambda r: r.created_at)
    assert len(rows) == 2
    assert json.loads(rows[0].routing_decision_json)["primary_domain"] == "reminders"  # ход1 — решение
    assert rows[1].routing_decision_json is None  # ход2 — СБРОШЕНО, не стейл reminders


# test_trace_debug_suppressed_when_trace_enabled УДАЛЁН (#138 Ф3-0): _persist_debug_turn
# и react_debug_turns снесены целиком — dual-write guard больше не существует.
@pytest.mark.asyncio
async def test_trace_never_breaks_turn(db_session, trace_on, monkeypatch):
    """Сбой записи трейса (finish) НЕ роняет ход — пользователь получает ответ."""
    def _boom(**kw):
        raise RuntimeError("trace db down")

    monkeypatch.setattr(react_trace_persist, "persist_trace_finish", _boom)
    u = seed_telegram_user(db_session)
    db_session.flush()
    stub = _RecordingStubLLM([AIMessage(content="Готово.", usage_metadata=_u(10, 5))])
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id="tr-nb",
        llm=stub, user_text="привет", inbound_message_id="m-nb",
        channel="telegram", provider_key="inception-mercury2")
    assert "Готово" in str(r)  # ход не упал, несмотря на сбой трейса


class _RaisingLLM:
    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, msgs):  # noqa: ANN001
        raise RuntimeError("LLM down")


@pytest.mark.asyncio
async def test_trace_error_branch_safe_reply(db_session, trace_on):
    """Handled-сбой графа → except handle_turn → строка done + outcome=safe_reply (не in_progress)."""
    SF = trace_on
    u = seed_telegram_user(db_session)
    db_session.flush()
    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id="tr-err",
        llm=_RaisingLLM(), user_text="привет", inbound_message_id="m-err",
        channel="telegram", provider_key="inception-mercury2")
    assert "потеряла контекст" in str(r)  # safe-reply
    rows = _trace_rows(SF, u.tenant_id)
    assert len(rows) == 1 and rows[0].status == "done" and rows[0].outcome == "safe_reply"
