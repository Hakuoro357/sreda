"""Phase 3c-3e integration: load_memories node + conversation.chat handler.

Uses a duck-typed fake LLM (no langchain inheritance; just responds
to ``bind_tools(...).invoke(messages)``) and a constant embedding
client (all vectors identical — cosine always 1.0 — so memory CRUD
plumbing is exercised without semantic search quality concerns).
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    Assistant,
    AssistantMemory,
    Tenant,
    User,
    Workspace,
)
from sreda.db.repositories.memory import MemoryRepository
from sreda.db.session import get_engine, get_session_factory
from sreda.runtime.dispatcher import ActionEnvelope, _resolve_command_action
from sreda.runtime.executor import ActionRuntimeService
from sreda.services.embeddings import FakeEmbeddingClient


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ConstantEmbeddingClient:
    """All inputs map to the same unit vector — cosine is always 1.0.
    Lets us test the save→recall plumbing without caring about semantic
    quality (that's what the live LM Studio smoke test is for)."""

    dim = 8

    def embed_document(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class _BoundFakeLLM:
    """What ``FakeLLM.bind_tools(...)`` returns. Responds to ``invoke``
    with the next scripted AIMessage."""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.idx = 0
        self.calls: list[list[Any]] = []
        self.tools: list[Any] = []

    def invoke(self, messages):
        self.calls.append(list(messages))
        if self.idx >= len(self.responses):
            # Fallback: final AIMessage with empty tool_calls so the
            # handler's loop terminates cleanly.
            return AIMessage(content="(fake: out of scripted responses)")
        msg = self.responses[self.idx]
        self.idx += 1
        return msg


class FakeLLM:
    """Duck-types just enough of ChatOpenAI to run our handler."""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._bound = _BoundFakeLLM(responses)

    def bind_tools(self, tools):
        self._bound.tools = list(tools)
        return self._bound

    @property
    def last_call(self) -> list[Any] | None:
        return self._bound.calls[-1] if self._bound.calls else None


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id: str, text: str, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"ok": True}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bootstrap(monkeypatch, tmp_path: Path, name: str):
    db_path = tmp_path / name
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    session.add(Tenant(id="t1", name="T"))
    session.add(Workspace(id="w1", tenant_id="t1", name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id="t1", workspace_id="w1", name="Sreda"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    session.commit()
    return session


def _chat_envelope(text: str) -> ActionEnvelope:
    return ActionEnvelope(
        action_type="conversation.chat",
        tenant_id="t1",
        workspace_id="w1",
        assistant_id="a1",
        user_id="u1",
        channel_type="telegram_dm",
        external_chat_id="42",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_message",
        source_value=text,
        params={"text": text},
    )


# ---------------------------------------------------------------------------
# Dispatcher fallback
# ---------------------------------------------------------------------------


def test_dispatcher_routes_free_text_to_conversation():
    assert _resolve_command_action("привет, как дела?") == (
        "conversation.chat",
        {"text": "привет, как дела?"},
    )


def test_dispatcher_slash_commands_take_priority():
    # Real command wins
    assert _resolve_command_action("/help") == ("help.show", {})
    # Unknown slash-command also falls through to conversation (LLM
    # will respond "I don't know that command")
    assert _resolve_command_action("/doesnotexist") == (
        "conversation.chat",
        {"text": "/doesnotexist"},
    )


def test_dispatcher_empty_returns_none():
    assert _resolve_command_action("") is None
    assert _resolve_command_action("   ") is None


# ---------------------------------------------------------------------------
# Handler behaviour
# ---------------------------------------------------------------------------


def test_conversation_without_llm_returns_fallback(monkeypatch, tmp_path: Path):
    """No MiMo key configured + no injected LLM → user gets a graceful
    "LLM not configured" reply instead of a crash."""
    monkeypatch.delenv("SREDA_MIMO_API_KEY", raising=False)
    monkeypatch.delenv("SREDA_MIMO_API_KEY_FILE", raising=False)
    session = _bootstrap(monkeypatch, tmp_path, "conv1.db")
    try:
        telegram = FakeTelegram()
        svc = ActionRuntimeService(session, telegram_client=telegram)
        queued = svc.enqueue_action(_chat_envelope("привет"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert len(telegram.sent) == 1
    assert "LLM пока не подключён" in telegram.sent[0]["text"]


def test_conversation_saves_core_fact_via_tool_call(monkeypatch, tmp_path: Path):
    """LLM emits a ``save_core_fact`` tool call — we verify the memory
    row lands in the DB with correct tier/source and the final AI
    message is delivered to the user."""
    session = _bootstrap(monkeypatch, tmp_path, "conv2.db")
    try:
        scripted = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_core_fact",
                        "args": {"content": "у меня дочь Маша 9 лет"},
                        "id": f"tc_{uuid4().hex[:8]}",
                    }
                ],
            ),
            AIMessage(content="Запомнил — дочь Маша, 9 лет."),
        ]
        fake_llm = FakeLLM(scripted)

        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=fake_llm,
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("у меня дочь Маша 9 лет"))
        asyncio.run(svc.process_job(queued.job_id))

        memories = session.query(AssistantMemory).all()
    finally:
        session.close()

    assert len(memories) == 1
    assert memories[0].tier == "core"
    assert memories[0].content == "у меня дочь Маша 9 лет"
    assert memories[0].source == "agent_inferred"

    assert len(telegram.sent) == 1
    assert "дочь Маша" in telegram.sent[0]["text"]


def test_conversation_saves_episode_via_tool_call(monkeypatch, tmp_path: Path):
    session = _bootstrap(monkeypatch, tmp_path, "conv3.db")
    try:
        scripted = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_episode",
                        "args": {"summary": "жалуется на сроки на работе"},
                        "id": "tc_1",
                    }
                ],
            ),
            AIMessage(content="Понял, зафиксировал."),
        ]
        svc = ActionRuntimeService(
            session,
            telegram_client=FakeTelegram(),
            llm_client=FakeLLM(scripted),
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("я сегодня не успеваю по срокам"))
        asyncio.run(svc.process_job(queued.job_id))

        memories = session.query(AssistantMemory).all()
    finally:
        session.close()

    assert len(memories) == 1
    assert memories[0].tier == "episodic"
    assert memories[0].content == "жалуется на сроки на работе"


def test_conversation_sees_loaded_memories_in_prompt(monkeypatch, tmp_path: Path):
    """Seeded memory should appear in the system message passed to the
    LLM (via ``load_memories`` node → state.memories → context._memories)."""
    session = _bootstrap(monkeypatch, tmp_path, "conv4.db")
    try:
        # Seed a core memory so recall returns it
        repo = MemoryRepository(session)
        emb = ConstantEmbeddingClient()
        repo.save(
            "t1",
            "u1",
            tier="core",
            content="у меня дочь Маша 9 лет",
            embedding=emb.embed_document("у меня дочь Маша 9 лет"),
            source="user_direct",
        )
        session.commit()

        fake_llm = FakeLLM([AIMessage(content="9 лет.")])
        svc = ActionRuntimeService(
            session,
            telegram_client=FakeTelegram(),
            llm_client=fake_llm,
            embedding_client=emb,
        )
        queued = svc.enqueue_action(_chat_envelope("сколько лет моей дочери?"))
        asyncio.run(svc.process_job(queued.job_id))

        # Inspect the system message the LLM was called with
        call_messages = fake_llm.last_call
    finally:
        session.close()

    assert call_messages is not None
    # First message is SystemMessage; its content must carry the memory
    system_content = call_messages[0].content
    assert "у меня дочь Маша 9 лет" in system_content


def test_conversation_loop_terminates_after_max_iterations(monkeypatch, tmp_path: Path):
    """A runaway LLM that keeps emitting tool calls must be stopped."""
    session = _bootstrap(monkeypatch, tmp_path, "conv5.db")
    try:
        # Six tool-call messages — the handler caps at 5 iterations
        scripted = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_episode",
                        "args": {"summary": f"summary {i}"},
                        "id": f"tc_{i}",
                    }
                ],
            )
            for i in range(6)
        ]
        svc = ActionRuntimeService(
            session,
            telegram_client=FakeTelegram(),
            llm_client=FakeLLM(scripted),
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("hi"))
        result = asyncio.run(svc.process_job(queued.job_id))

        memory_count = session.query(AssistantMemory).count()
    finally:
        session.close()

    assert result == "completed"
    # Handler caps at 5 iterations, so at most 5 tool calls executed
    assert memory_count <= 5


# ---------------------------------------------------------------------------
# Acceptance test: save → new invocation → recall
# ---------------------------------------------------------------------------


def test_acceptance_fact_persists_across_invocations(monkeypatch, tmp_path: Path):
    """Plan §Phase 3 acceptance:
    'В одном thread сообщить факт ... в новом thread через сутки спросить
    → агент отвечает без переспроса.'

    Simplified: one invocation saves the fact via tool call, a later
    invocation (fresh graph run) retrieves it and surfaces it to the
    LLM. We don't test the LLM's reasoning (fake responses), we test
    that the plumbing passes memory through.
    """
    session = _bootstrap(monkeypatch, tmp_path, "conv_acc.db")
    emb = ConstantEmbeddingClient()
    try:
        # --- Invocation 1: user states fact, LLM saves via tool call
        scripted_save = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_core_fact",
                        "args": {"content": "у меня дочь Маша 9 лет"},
                        "id": "tc_save",
                    }
                ],
            ),
            AIMessage(content="Записал."),
        ]
        save_llm = FakeLLM(scripted_save)
        svc = ActionRuntimeService(
            session,
            telegram_client=FakeTelegram(),
            llm_client=save_llm,
            embedding_client=emb,
        )
        queued_save = svc.enqueue_action(_chat_envelope("у меня дочь Маша 9 лет"))
        asyncio.run(svc.process_job(queued_save.job_id))

        # Fact is in DB
        saved = session.query(AssistantMemory).filter_by(tier="core").one()
        assert saved.content == "у меня дочь Маша 9 лет"

        # --- Invocation 2 (fresh run_id): user asks, LLM answers using memory
        recall_llm = FakeLLM([AIMessage(content="9 лет, Маше 9.")])
        svc2 = ActionRuntimeService(
            session,
            telegram_client=FakeTelegram(),
            llm_client=recall_llm,
            embedding_client=emb,
        )
        queued_q = svc2.enqueue_action(_chat_envelope("сколько лет моей дочери?"))
        asyncio.run(svc2.process_job(queued_q.job_id))

        call_msgs = recall_llm.last_call
    finally:
        session.close()

    assert call_msgs is not None
    # Memory surfaced in the system prompt of the second invocation —
    # the LLM saw the fact without the user needing to restate it
    assert "у меня дочь Маша 9 лет" in call_msgs[0].content

    # Access count should have been bumped by load_memories touch
    # (in the second invocation). Re-opening session to verify.
    engine = get_engine()
    sess = get_session_factory()()
    try:
        refreshed = sess.query(AssistantMemory).filter_by(tier="core").one()
        assert refreshed.access_count >= 1
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Tools direct tests (save_core_fact / save_episode / recall_memory)
# ---------------------------------------------------------------------------


def test_tools_write_memories_with_correct_tier(monkeypatch, tmp_path: Path):
    """Invoke tools directly (bypass the LLM) to verify side-effects."""
    from sreda.runtime.tools import build_memory_tools

    session = _bootstrap(monkeypatch, tmp_path, "conv_tools.db")
    try:
        emb = ConstantEmbeddingClient()
        tools = build_memory_tools(
            session=session, tenant_id="t1", user_id="u1", embedding_client=emb
        )
        by_name = {t.name: t for t in tools}

        r1 = by_name["save_core_fact"].invoke({"content": "live in Moscow"})
        r2 = by_name["save_episode"].invoke({"summary": "bad day"})
        r3 = by_name["recall_memory"].invoke({"query": "foo", "top_k": 3})

        assert r1.startswith("saved_core:")
        assert r2.startswith("saved_episode:")
        hits = json.loads(r3)
        contents = {h["content"] for h in hits}
    finally:
        session.close()

    assert "live in Moscow" in contents
    assert "bad day" in contents
