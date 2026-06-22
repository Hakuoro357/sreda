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
