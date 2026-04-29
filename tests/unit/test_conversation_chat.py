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
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.repositories.memory import MemoryRepository
from sreda.db.session import get_engine, get_session_factory
from sreda.features.app_registry import get_feature_registry
from sreda.features.skill_contracts import (
    SkillLifecycleStatus,
    SkillManifestBase,
)
from sreda.runtime.dispatcher import ActionEnvelope, _resolve_command_action
from sreda.runtime.executor import ActionRuntimeService
from sreda.services.embeddings import FakeEmbeddingClient


TEST_CHAT_FEATURE_KEY = "test_chat_skill"


class _TestChatFeature:
    """Minimal feature module with ``provides_chat=True`` for tests."""

    feature_key = TEST_CHAT_FEATURE_KEY

    def register_api(self, app):
        pass

    def register_runtime(self):
        pass

    def register_workers(self):
        pass

    def get_manifest(self):
        return SkillManifestBase(
            feature_key=TEST_CHAT_FEATURE_KEY,
            title="Test Chat",
            description="Chat skill used by tests.",
            default_status=SkillLifecycleStatus.active,
            provides_chat=True,
            default_credits_monthly_quota=1_000_000,
        )


def _register_chat_skill_once():
    """Install the test chat manifest into the process-wide registry
    if it isn't there yet. Safe to call from multiple tests."""
    registry = get_feature_registry()
    if registry.get_manifest(TEST_CHAT_FEATURE_KEY) is None:
        registry.register(_TestChatFeature())


def _seed_chat_subscription(session, *, credits_quota: int | None = 1_000_000):
    """Give tenant t1 an active subscription to the test chat skill."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=f"{TEST_CHAT_FEATURE_KEY}_basic",
        feature_key=TEST_CHAT_FEATURE_KEY,
        title="Test Chat Basic",
        description="",
        price_rub=300,
        credits_monthly_quota=credits_quota,
    )
    session.add(plan)
    session.flush()
    sub = TenantSubscription(
        id=f"sub_{uuid4().hex[:16]}",
        tenant_id="t1",
        plan_id=plan.id,
        status="active",
        starts_at=datetime.now(timezone.utc) - timedelta(days=1),
        active_until=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(sub)
    session.commit()


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
    """Duck-types just enough of ChatOpenAI to run our handler.

    Exposes both ``bind_tools(...).invoke(...)`` (normal tool-loop path)
    and a direct ``invoke(...)`` (used by the exhaustion fallback in
    ``execute_conversation_chat`` — one final summary call WITHOUT
    tools bound). Both paths pull from the same scripted-response
    queue so tests can mix them naturally."""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._bound = _BoundFakeLLM(responses)

    def bind_tools(self, tools):
        self._bound.tools = list(tools)
        return self._bound

    def invoke(self, messages):
        # Same queue as the bound object — keeps call-order clear when
        # a test scripts both tool-call and final-summary responses.
        return self._bound.invoke(messages)

    @property
    def last_call(self) -> list[Any] | None:
        return self._bound.calls[-1] if self._bound.calls else None


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id: str, text: str, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bootstrap(
    monkeypatch,
    tmp_path: Path,
    name: str,
    *,
    seed_subscription: bool = True,
    credits_quota: int | None = 1_000_000,
):
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

    # Phase 4.5: conversation.chat requires a chat-capable skill + active
    # subscription. Register and seed both by default; tests that want
    # to exercise the "no subscription" path pass seed_subscription=False.
    _register_chat_skill_once()
    if seed_subscription:
        _seed_chat_subscription(session, credits_quota=credits_quota)
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


def test_conversation_loop_terminates_naturally_before_cap(monkeypatch, tmp_path: Path):
    """An LLM that emits fewer tool-calls than the cap must terminate
    cleanly when it finally returns plain text — WITHOUT invoking the
    exhaustion-summary fallback. Keeps budget usage tight on simple
    turns."""
    session = _bootstrap(monkeypatch, tmp_path, "conv5.db")
    try:
        # Six tool-call messages, then a final plain-text reply. The
        # handler's cap is 8 iterations; here the loop exits naturally
        # at iter=6 on the plain-text message.
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
        scripted.append(AIMessage(content="Готово — сохранил всё."))
        fake_llm = FakeLLM(scripted)
        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=fake_llm,
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("hi"))
        result = asyncio.run(svc.process_job(queued.job_id))

        memory_count = session.query(AssistantMemory).count()
    finally:
        session.close()

    assert result == "completed"
    # 6 tool-calls fired, one plain-text message delivered, loop did
    # NOT hit the cap — exactly 7 invokes total.
    assert memory_count == 6
    assert fake_llm._bound.idx == 7
    assert telegram.sent[0]["text"] == "Готово — сохранил всё."


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


# ---------------------------------------------------------------------------
# Phase 4.5: per-skill budget attribution
# ---------------------------------------------------------------------------


def test_conversation_without_subscription_returns_upsell(monkeypatch, tmp_path: Path):
    """No chat-skill subscription → do NOT call the LLM; reply with
    upsell prompt. This keeps users out of the expensive path until
    they've paid for at least one chat-capable skill."""
    session = _bootstrap(monkeypatch, tmp_path, "cb_nosub.db", seed_subscription=False)
    try:
        # FakeLLM with no scripted responses — if the handler calls it,
        # we'll see an index-error. Presence of zero responses = proof
        # of the no-LLM path.
        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=FakeLLM([]),
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("привет"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert len(telegram.sent) == 1
    assert "подписк" in telegram.sent[0]["text"].lower()


def test_conversation_exhausted_budget_returns_upgrade_prompt(monkeypatch, tmp_path: Path):
    """Subscription exists but quota is fully consumed → reply with
    quota-exhausted message + inline upgrade button, no LLM call."""
    session = _bootstrap(
        monkeypatch, tmp_path, "cb_exhausted.db",
        seed_subscription=True, credits_quota=100,  # tiny quota
    )
    try:
        # Pre-fill usage so the quota is exhausted.
        from sreda.services.budget import BudgetService
        BudgetService(session).record_llm_usage(
            tenant_id="t1", feature_key=TEST_CHAT_FEATURE_KEY,
            model="mimo-v2-pro", prompt_tokens=100, completion_tokens=0,
            run_id="run_seed",
        )
        session.commit()

        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=FakeLLM([]),  # must not be called
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("привет"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert len(telegram.sent) == 1
    msg = telegram.sent[0]
    assert "исчерпан" in msg["text"].lower()
    # Upgrade CTA present as inline button
    assert msg["reply_markup"] is not None
    btn_labels = [
        btn.get("callback_data", "")
        for row in msg["reply_markup"]["inline_keyboard"]
        for btn in row
    ]
    assert any("buy_extra" in cd for cd in btn_labels)


def test_conversation_records_llm_usage_in_skill_ai_executions(monkeypatch, tmp_path: Path):
    """After an LLM call, skill_ai_executions should have a row with
    the right tenant/feature/model/credits_consumed."""
    from langchain_core.messages import AIMessage
    from sreda.db.models.skill_platform import SkillAIExecution

    session = _bootstrap(monkeypatch, tmp_path, "cb_usage.db", seed_subscription=True)
    try:
        # Scripted AI response carrying usage_metadata the handler
        # should pick up and record.
        msg = AIMessage(content="ok", usage_metadata={"input_tokens": 120, "output_tokens": 80, "total_tokens": 200})
        fake_llm = FakeLLM([msg])
        # Pretend model is mimo-v2-pro so credits = 200*2 = 400.
        monkeypatch.setenv("SREDA_MIMO_CHAT_MODEL", "mimo-v2-pro")
        get_settings.cache_clear()

        svc = ActionRuntimeService(
            session,
            telegram_client=FakeTelegram(),
            llm_client=fake_llm,
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("hi"))
        asyncio.run(svc.process_job(queued.job_id))

        rows = session.query(SkillAIExecution).all()
    finally:
        session.close()

    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == "t1"
    assert row.feature_key == TEST_CHAT_FEATURE_KEY
    assert row.prompt_tokens == 120
    assert row.completion_tokens == 80
    assert row.credits_consumed == 400  # 200 tokens × 2 (pro rate)


def test_tool_loop_exhaustion_forces_summary_turn(monkeypatch, tmp_path: Path):
    """Regression: if the model keeps calling tools past the budget,
    the handler MUST force one tool-less summary call so the user gets
    a real reply instead of the "couldn't form answer" stub.

    Reproduces the weather-on-Schodnya case from 2026-04-18 where the
    LLM cycled wttr.in formats for 5 rounds and the user got a dead
    reply despite having the data.
    """
    session = _bootstrap(monkeypatch, tmp_path, "conv_exhaust.db")
    try:
        # Script _MAX_TOOL_ITERATIONS=12 tool-call responses so the
        # loop exhausts exactly as in prod. Then a plain-text response
        # which the forced summary invoke must pick up.
        def _tc_response(i: int) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_episode",
                        "args": {"summary": f"iter {i}"},
                        "id": f"tc_{i}",
                    }
                ],
            )

        scripted = [_tc_response(i) for i in range(12)]
        scripted.append(
            AIMessage(content="На основе собранных данных: дождь идёт весь день.")
        )
        fake_llm = FakeLLM(scripted)

        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=fake_llm,
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("долго будет идти дождь?"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert len(telegram.sent) == 1
    sent_text = telegram.sent[0]["text"]
    # The forced-summary response is what the user must see — NOT the
    # legacy "couldn't form answer" stub.
    assert "дождь идёт весь день" in sent_text
    assert "слишком много шагов" not in sent_text

    # Exactly 13 LLM invocations: 12 in-loop + 1 forced summary.
    assert fake_llm._bound.idx == 13


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


# ---------------------------------------------------------------------------
# Hallucination detector integration (handler-level retry mechanism)
# ---------------------------------------------------------------------------
# Эти тесты проверяют что детектор `detect_unbacked_claim` действительно
# триггерит retry внутри `execute_conversation_chat`, а не только в
# unit-тестах самой функции. Сценарий:
#   iter 0 → AIMessage(content="Сохранила рецепт", tool_calls=[])
#   handler: detect_unbacked_claim → True
#         → injects HumanMessage(nudge) → continues loop
#   iter 1 → AIMessage(tool_calls=[save_core_fact])
#   handler: runs tool → continues
#   iter 2 → AIMessage(content="Запомнила.")  — финальный summary
# Сравнивается с happy-path где iter 0 сразу делает tool_call.


def test_hallucination_triggers_one_retry(monkeypatch, tmp_path: Path):
    """LLM в первой итерации описывает действие текстом без tool_call →
    handler детектит claim, инжектит nudge, перезапускает iteration.
    Конечный результат: write-tool вызван, юзер получает финальный текст."""
    session = _bootstrap(monkeypatch, tmp_path, "halluc_retry.db")
    try:
        scripted = [
            # iter 0: hallucination — claim без tool_call
            AIMessage(
                content="Готово! Сохранила рецепт борща в твою книгу.",
                tool_calls=[],
            ),
            # iter 1: после nudge'а — реальный tool call
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_core_fact",
                        "args": {"content": "рецепт борща"},
                        "id": f"tc_{uuid4().hex[:8]}",
                    }
                ],
            ),
            # iter 2: финальный текст после исполнения tool'а
            AIMessage(content="Запомнила рецепт борща."),
        ]
        fake_llm = FakeLLM(scripted)
        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=fake_llm,
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("сохрани рецепт борща"))
        asyncio.run(svc.process_job(queued.job_id))

        memories = session.query(AssistantMemory).all()
    finally:
        session.close()

    # Все 3 scripted-response'а потреблены — handler сделал retry
    assert fake_llm._bound.idx == 3, (
        f"expected 3 invocations (hallucination + retry + final summary), "
        f"got {fake_llm._bound.idx}"
    )
    # Tool реально вызван — память записана
    assert len(memories) == 1
    assert memories[0].content == "рецепт борща"
    # Юзер получил финальный текст (НЕ галлюцинированный первый)
    assert len(telegram.sent) == 1
    assert "запомнила" in telegram.sent[0]["text"].lower()


def test_no_hallucination_no_retry(monkeypatch, tmp_path: Path):
    """Happy path: LLM сразу делает tool_call в iter 0. Handler НЕ
    делает retry — детектор пропускает (есть write-tool в called_tools)."""
    session = _bootstrap(monkeypatch, tmp_path, "halluc_skip.db")
    try:
        scripted = [
            # iter 0: сразу tool_call, никакой галлюцинации
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_core_fact",
                        "args": {"content": "рецепт супа"},
                        "id": "tc_1",
                    }
                ],
            ),
            # iter 1: финальный текст
            AIMessage(content="Записала рецепт супа в книгу."),
        ]
        fake_llm = FakeLLM(scripted)
        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=fake_llm,
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("запиши рецепт супа"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    # Только 2 invocation'а — retry не делался
    assert fake_llm._bound.idx == 2, (
        f"expected 2 invocations (tool_call + final summary), no retry — "
        f"got {fake_llm._bound.idx}"
    )
    assert len(telegram.sent) == 1


def test_hallucination_retry_bounded_to_one(monkeypatch, tmp_path: Path):
    """Если LLM повторно галлюцинирует после nudge'а — handler НЕ делает
    второй retry, а принимает текст как есть. `_hallucination_nudged`
    флаг не позволяет уйти в бесконечный loop."""
    session = _bootstrap(monkeypatch, tmp_path, "halluc_bounded.db")
    try:
        scripted = [
            # iter 0: claim без tool — fire detector
            AIMessage(
                content="Сохранила рецепт борща в книгу.",
                tool_calls=[],
            ),
            # iter 1: ОПЯТЬ claim без tool — должен приниматься как final
            AIMessage(
                content="Готово! Записала рецепт.",
                tool_calls=[],
            ),
        ]
        fake_llm = FakeLLM(scripted)
        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=fake_llm,
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("сохрани рецепт"))
        asyncio.run(svc.process_job(queued.job_id))

        memories = session.query(AssistantMemory).all()
    finally:
        session.close()

    # Ровно 2 invocation'а — один retry, не больше
    assert fake_llm._bound.idx == 2, (
        f"expected exactly 2 invocations (hallucination + ONE retry), "
        f"got {fake_llm._bound.idx}"
    )
    # Tool НЕ вызывался (LLM повторно соврал) — память пуста
    assert len(memories) == 0
    # Юзер получил второе (последнее) сообщение — handler не блокирует
    # ответ при повторной галлюцинации, иначе юзер останется без reply'я
    assert len(telegram.sent) == 1
