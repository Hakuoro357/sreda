"""Unit tests for _load_chat_history — the helper that rebuilds the
last N turns of a conversation so the LLM has context."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import OutboxMessage, Tenant, Workspace
from sreda.db.models.runtime import AgentRun, AgentThread
from sreda.runtime.handlers import _load_chat_history


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="tenant_1", name="Test"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Default"))
    session.add(
        AgentThread(
            id="thread_1",
            tenant_id="tenant_1",
            workspace_id="workspace_1",
            channel_type="telegram",
            external_chat_id="100",
            status="active",
        )
    )
    session.commit()
    return session


def _make_turn(
    session,
    *,
    run_id: str,
    thread_id: str = "thread_1",
    user_text: str,
    bot_text: str,
    status: str = "completed",
    action_type: str = "conversation.chat",
    created_at: datetime | None = None,
):
    """Seed a completed AgentRun + the OutboxMessage rows it produced."""
    outbox_ids: list[str] = []
    if bot_text:
        outbox_id = f"out_{run_id}"
        session.add(
            OutboxMessage(
                id=outbox_id,
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                channel_type="telegram",
                status="sent",
                payload_json=json.dumps({"chat_id": "100", "text": bot_text}),
            )
        )
        outbox_ids.append(outbox_id)

    run = AgentRun(
        id=run_id,
        thread_id=thread_id,
        tenant_id="tenant_1",
        workspace_id="workspace_1",
        action_type=action_type,
        status=status,
        input_json=json.dumps({"params": {"text": user_text}}),
        result_json=json.dumps({"outbox_message_ids": outbox_ids, "reply_count": len(outbox_ids)}),
    )
    if created_at is not None:
        run.created_at = created_at
    session.add(run)
    session.commit()
    return run


def test_loads_prior_turns_newest_first() -> None:
    session = _fresh_session()
    base_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _make_turn(session, run_id="r1", user_text="Привет", bot_text="Здравствуй", created_at=base_ts)
    _make_turn(session, run_id="r2", user_text="Как дела?", bot_text="Отлично", created_at=base_ts + timedelta(seconds=1))
    _make_turn(session, run_id="r3", user_text="Где я живу?", bot_text="Не знаю", created_at=base_ts + timedelta(seconds=2))
    current = _make_turn(
        session, run_id="r4",
        user_text="В Москве", bot_text="",
        status="pending",
        created_at=base_ts + timedelta(seconds=3),
    )

    history = _load_chat_history(session, current.id)

    assert [u for u, _ in history] == ["Где я живу?", "Как дела?", "Привет"]
    assert [b for _, b in history] == ["Не знаю", "Отлично", "Здравствуй"]


def test_skips_current_run() -> None:
    session = _fresh_session()
    _make_turn(session, run_id="prior", user_text="Было", bot_text="Ответ")
    current = _make_turn(session, run_id="now", user_text="Текущее", bot_text="", status="pending")

    history = _load_chat_history(session, current.id)

    assert len(history) == 1
    assert history[0] == ("Было", "Ответ")


def test_skips_incomplete_runs() -> None:
    session = _fresh_session()
    _make_turn(session, run_id="failed", user_text="Упало", bot_text="", status="failed")
    _make_turn(session, run_id="ok", user_text="Нормально", bot_text="Принято")
    current = _make_turn(session, run_id="now", user_text="Текущее", bot_text="", status="pending")

    history = _load_chat_history(session, current.id)

    assert history == [("Нормально", "Принято")]


def test_skips_non_conversation_actions() -> None:
    session = _fresh_session()
    _make_turn(session, run_id="skill", user_text="/help", bot_text="manual", action_type="help.show")
    _make_turn(session, run_id="chat", user_text="Привет", bot_text="Здравствуй")
    current = _make_turn(session, run_id="now", user_text="Текущее", bot_text="", status="pending")

    history = _load_chat_history(session, current.id)

    assert history == [("Привет", "Здравствуй")]


def test_respects_limit() -> None:
    session = _fresh_session()
    base_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    for i in range(15):
        _make_turn(
            session, run_id=f"r{i}",
            user_text=f"U{i}", bot_text=f"B{i}",
            created_at=base_ts + timedelta(seconds=i),
        )
    current = _make_turn(
        session, run_id="cur", user_text="now", bot_text="", status="pending",
        created_at=base_ts + timedelta(seconds=100),
    )

    history = _load_chat_history(session, current.id, limit=5)

    assert len(history) == 5
    # Newest five: U14, U13, U12, U11, U10
    assert [u for u, _ in history] == ["U14", "U13", "U12", "U11", "U10"]


def test_returns_empty_when_no_prior_turns() -> None:
    session = _fresh_session()
    current = _make_turn(session, run_id="first", user_text="Hi", bot_text="", status="pending")

    history = _load_chat_history(session, current.id)

    assert history == []


def test_returns_empty_on_unknown_run_id() -> None:
    session = _fresh_session()
    history = _load_chat_history(session, "run_does_not_exist")
    assert history == []
