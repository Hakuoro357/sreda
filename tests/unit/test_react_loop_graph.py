"""Фаза 0 #162 — интеграция графа react_loop (стаб-LLM, без сети).

Проверяет:
  - срез = ровно 12 инструментов с УРЕЗАННЫМИ схемами (чеклист п.6,9);
  - граф реально биндит ToolRuntimeContext в run_tools на создание → ctx-ветка
    сервиса срабатывает (operation_id проставлен; легаси оставил бы None).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from langchain_core.messages import AIMessage

from sreda.db.models.housewife import FamilyReminder
from sreda.runtime import react_loop
from tests.unit.conftest import seed_telegram_user


class _StubLLM:
    """Скриптованный LLM: bind_tools → self; invoke → следующий AIMessage из сценария."""

    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted = scripted
        self._i = 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


def test_build_slice_tools_names_and_reduced_schemas(db_session):
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = react_loop.build_slice_tools(db_session, u.tenant_id, u.user_id)
    names = {t.name for t in tools}
    assert names == {
        "list_reminders", "schedule_reminder", "update_reminder", "cancel_reminder",
        "list_tasks", "add_task", "update_task", "complete_task", "uncomplete_task",
        "cancel_task", "delete_task", "ask_human",
    }, names
    by = {t.name: t for t in tools}
    add_args = set(by["add_task"].args.keys())
    assert "details_items" not in add_args, add_args
    assert "reminder_offset_minutes" not in add_args, add_args
    upd_args = set(by["update_task"].args.keys())
    assert "scheduled_date" not in upd_args, upd_args
    assert "time_start" not in upd_args, upd_args
    assert "recurrence_rule" not in upd_args, upd_args


@pytest.mark.asyncio
async def test_graph_create_uses_ctx_branch(db_session):
    """Граф проводит создание через ctx-ветку (operation_id проставлен)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "schedule_reminder",
            "args": {"title": "разминка", "trigger_iso": "2030-01-01T06:00:00+00:00"},
            "id": "call_1",
        }]),
        AIMessage(content="Готово, поставила напоминание."),
    ]
    reply = await react_loop.handle_turn(
        session=db_session,
        tenant_id=u.tenant_id,
        user_id=u.user_id,
        thread_id=f"react:test:{uuid4().hex}",
        llm=_StubLLM(scripted),
        user_text="напомни про разминку завтра в 6",
        inbound_message_id=f"msg_{uuid4().hex}",
        channel="max",
    )
    rows = (
        db_session.query(FamilyReminder)
        .filter_by(tenant_id=u.tenant_id, user_id=u.user_id)
        .all()
    )
    assert len(rows) == 1, f"ожидали 1 напоминание, reply={reply!r}"
    assert rows[0].operation_id is not None, (
        "ctx-ветка не сработала — operation_id пуст (граф не забиндил ctx)"
    )
