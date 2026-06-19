"""vex#170: debug-сохранение ходов react (вопрос + ответ + инструменты), allowlist-гейт.

Проверяет: _called_tools берёт ТОЛЬКО текущий ход (после последнего HumanMessage); запись идёт
лишь для тенантов из allowlist react_debug_tenants (чужой тенант → не пишем); запись пишет строку
с расшифрованным текстом + инструментами + kind.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.runtime import react_loop
import sreda.db.models  # noqa: F401 — регистрация таблиц
from sreda.db.models import ReactDebugTurn


def _fresh_factory():
    eng = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)


def test_called_tools_current_turn_only():
    # история треда: прошлый ход (delete_recipe) + текущий (после HumanMessage)
    result = {"messages": [
        HumanMessage("удали рецепт"),
        AIMessage(content="", tool_calls=[{"name": "delete_recipe", "args": {}, "id": "0"}]),
        ToolMessage(content="ok", name="delete_recipe", tool_call_id="0"),
        AIMessage(content="готово"),
        HumanMessage("покажи задачи"),  # ← текущий ход начинается тут
        AIMessage(content="", tool_calls=[{"name": "list_tasks", "args": {}, "id": "1"}]),
        AIMessage(content="вот"),
    ]}
    assert react_loop._called_tools(result) == ["list_tasks"]  # без delete_recipe из прошлого хода
    assert react_loop._called_tools({}) == []
    assert react_loop._called_tools(None) == []


def test_persist_tenant_not_in_allowlist_is_noop(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_DEBUG_TENANTS", "other_tenant")
    get_settings.cache_clear()
    called = []
    monkeypatch.setattr("sreda.db.session.get_session_factory", lambda: called.append(1))
    react_loop._persist_debug_turn(tenant_id="t", user_id="u", thread_id="th", channel="max",
                                   user_text="привет", reply="ответ", tools=["x"], kind="final")
    assert called == [], "тенант не в allowlist → фабрика сессий не должна вызываться"
    get_settings.cache_clear()


def test_persist_allowlisted_writes_turn(monkeypatch):
    monkeypatch.setenv("SREDA_REACT_DEBUG_TENANTS", "t1,t2")
    get_settings.cache_clear()
    SF = _fresh_factory()
    monkeypatch.setattr("sreda.db.session.get_session_factory", lambda: SF)
    react_loop._persist_debug_turn(
        tenant_id="t1", user_id="u1", thread_id="react:t1:c", channel="max",
        user_text="покажи задачи", reply=react_loop._Reply("Вот твои задачи."),
        tools=["list_tasks"], kind="final")
    get_settings.cache_clear()

    rows = SF().query(ReactDebugTurn).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.user_text == "покажи задачи"        # читается (в тестах без ключа — plaintext)
    assert r.reply_text == "Вот твои задачи."    # _Reply — подкласс str → чистый текст
    assert "list_tasks" in (r.tools_json or "")
    assert r.kind == "final" and r.channel == "max"
    assert r.tenant_id == "t1" and r.user_id == "u1"


@pytest.mark.asyncio
async def test_handle_turn_persists_for_allowlisted(monkeypatch, db_session):
    """End-to-end: тенант в allowlist → handle_turn пишет debug-строку (final) вопрос+ответ."""
    from uuid import uuid4
    from tests.unit.conftest import seed_telegram_user
    u = seed_telegram_user(db_session); db_session.commit()
    monkeypatch.setenv("SREDA_REACT_DEBUG_TENANTS", u.tenant_id)
    get_settings.cache_clear()
    SF = _fresh_factory()
    monkeypatch.setattr("sreda.db.session.get_session_factory", lambda: SF)

    class _Stub:
        def bind_tools(self, t):  # noqa: ANN001
            return self

        def invoke(self, m):  # noqa: ANN001
            return AIMessage(content="Вот, что нашла.")

    r = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:t:{uuid4().hex}", llm=_Stub(), user_text="что у меня сегодня",
        inbound_message_id="m1", channel="telegram")
    get_settings.cache_clear()

    rows = SF().query(ReactDebugTurn).filter_by(tenant_id=u.tenant_id).all()
    assert len(rows) == 1
    assert rows[0].user_text == "что у меня сегодня"
    assert rows[0].reply_text == str(r)
    assert rows[0].kind == "final"
