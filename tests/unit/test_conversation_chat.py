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

from langchain_core.messages import AIMessage, AIMessageChunk

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    Assistant,
    AssistantMemory,
    OutboxMessage,
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
from sreda.services.ack_messages import FINAL_PROGRESS_TEXT


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
    """Give tenant t1 active subscriptions for gate + test chat skill."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    housewife_plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=f"housewife_assistant_basic_{uuid4().hex[:8]}",
        feature_key="housewife_assistant",
        title="Housewife Basic",
        description="",
        price_rub=0,
        credits_monthly_quota=1_000_000,
    )
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=f"{TEST_CHAT_FEATURE_KEY}_basic",
        feature_key=TEST_CHAT_FEATURE_KEY,
        title="Test Chat Basic",
        description="",
        price_rub=300,
        credits_monthly_quota=credits_quota,
    )
    session.add(housewife_plan)
    session.add(plan)
    session.flush()
    session.add(
        TenantSubscription(
            id=f"sub_{uuid4().hex[:16]}",
            tenant_id="t1",
            plan_id=housewife_plan.id,
            feature_key="housewife_assistant",
            status="active",
            starts_at=datetime.now(timezone.utc) - timedelta(days=1),
            active_until=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    sub = TenantSubscription(
        id=f"sub_{uuid4().hex[:16]}",
        tenant_id="t1",
        plan_id=plan.id,
        feature_key=TEST_CHAT_FEATURE_KEY,
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


class _BoundStreamingFinalFakeLLM(_BoundFakeLLM):
    def stream(self, messages):
        msg = self.invoke(messages)
        if getattr(msg, "tool_calls", None):
            for idx, tc in enumerate(msg.tool_calls):
                yield AIMessageChunk(
                    content="",
                    additional_kwargs=msg.additional_kwargs,
                    tool_call_chunks=[{
                        "name": tc.get("name"),
                        "args": json.dumps(tc.get("args") or {}, ensure_ascii=False),
                        "id": tc.get("id"),
                        "index": idx,
                    }],
                )
            return
        content = str(msg.content or "")
        midpoint = max(1, len(content) // 2)
        yield AIMessageChunk(content=content[:midpoint])
        yield AIMessageChunk(content=content[midpoint:])


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


class StreamingFinalFakeLLM(FakeLLM):
    def __init__(self, responses: list[AIMessage]) -> None:
        self._bound = _BoundStreamingFinalFakeLLM(responses)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[dict] = []

    async def send_message(self, chat_id: str, text: str, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}

    async def edit_message_text(self, *, chat_id, message_id, text, reply_markup=None):
        self.edited.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
        })
        return {"ok": True}

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append({"chat_id": chat_id, "message_id": message_id})
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


def _max_chat_envelope(text: str) -> ActionEnvelope:
    env = _chat_envelope(text)
    return ActionEnvelope(
        action_type=env.action_type,
        tenant_id=env.tenant_id,
        workspace_id=env.workspace_id,
        assistant_id=env.assistant_id,
        user_id=env.user_id,
        channel_type="max_dm",
        external_chat_id="max-chat",
        bot_key=env.bot_key,
        inbound_message_id=env.inbound_message_id,
        source_type="max_message",
        source_value=env.source_value,
        params=env.params,
    )


def _message_content_text(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


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
                additional_kwargs={"reasoning_content": "thinking trace"},
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


def test_conversation_streams_final_answer_into_ack_after_tool_call(
    monkeypatch, tmp_path: Path
):
    session = _bootstrap(monkeypatch, tmp_path, "conv_stream.db")
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
                additional_kwargs={"reasoning_content": "thinking trace"},
            ),
            AIMessage(content="Запомнил — дочь Маша, 9 лет."),
        ]
        fake_llm = StreamingFinalFakeLLM(scripted)
        telegram = FakeTelegram()

        from sreda.services.ack_progress import TelegramAckProgressController

        ack_progress = TelegramAckProgressController(
            telegram_client=telegram,
            chat_id="100000001",
            ack_message_id_future=555,
            enabled=True,
        )
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=fake_llm,
            embedding_client=ConstantEmbeddingClient(),
            ack_progress_controller=ack_progress,
        )
        queued = svc.enqueue_action(_chat_envelope("у меня дочь Маша 9 лет"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    edited_texts = [item["text"] for item in telegram.edited]
    assert telegram.sent == []
    assert any(
        text.startswith("Запомнил") and text != "Запомнил — дочь Маша, 9 лет."
        for text in edited_texts
    )
    assert edited_texts[-1] == "Запомнил — дочь Маша, 9 лет."
    assert FINAL_PROGRESS_TEXT not in edited_texts


def test_conversation_uses_structured_menu_render_after_list_menu(
    monkeypatch, tmp_path: Path
):
    session = _bootstrap(monkeypatch, tmp_path, "conv_menu_render.db")
    try:
        monkeypatch.setattr(
            "sreda.runtime.handlers._resolve_chat_feature_key",
            lambda _session, _tenant_id: "housewife_assistant",
        )
        from sreda.services.housewife_chat_tools import build_housewife_tools

        tools = {
            t.name: t
            for t in build_housewife_tools(
                session=session, tenant_id="t1", user_id="u1"
            )
        }
        plan = tools["plan_week_menu"].invoke({
            "week_start": "2026-04-20",
            "days": [
                {
                    "day_of_week": 0,
                    "meals": {
                        "breakfast": {"free_text": "овсянка"},
                        "lunch": {"free_text": "суп"},
                        "dinner": {"free_text": "плов"},
                    },
                },
                {
                    "day_of_week": 1,
                    "meals": {
                        "breakfast": {"free_text": "творог"},
                        "lunch": {"free_text": "борщ"},
                        "dinner": {"free_text": "рыба"},
                    },
                },
            ],
        })
        assert plan.startswith("ok:plan_created:")

        glued_bad_final = (
            "Меню на неделю 20–26 апреля: Понедельник, 20 апреля:\n"
            "• Завтрак: овсянка\n"
            "• Обед: суп\n"
            "• Ужин: плов Вторник, 21 апреля:\n"
            "• Завтрак: творог\n"
            "• Обед: борщ\n"
            "• Ужин: рыба Собрать список покупок?"
        )
        scripted = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_menu",
                        "args": {"week_start": "2026-04-20"},
                        "id": f"tc_{uuid4().hex[:8]}",
                    }
                ],
                additional_kwargs={"reasoning_content": "thinking trace"},
            ),
            AIMessage(content=glued_bad_final),
        ]
        telegram = FakeTelegram()

        from sreda.services.ack_progress import TelegramAckProgressController

        ack_progress = TelegramAckProgressController(
            telegram_client=telegram,
            chat_id="100000001",
            ack_message_id_future=555,
            enabled=True,
        )
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=StreamingFinalFakeLLM(scripted),
            embedding_client=ConstantEmbeddingClient(),
            ack_progress_controller=ack_progress,
        )
        queued = svc.enqueue_action(_chat_envelope("покажи меню на неделю"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    expected = (
        "Меню на неделю 20–26 апреля:\n\n"
        "Понедельник, 20 апреля\n"
        "• Завтрак: овсянка\n"
        "• Обед: суп\n"
        "• Ужин: плов\n\n"
        "Вторник, 21 апреля\n"
        "• Завтрак: творог\n"
        "• Обед: борщ\n"
        "• Ужин: рыба\n\n"
        "Собрать список покупок?"
    )
    edited_texts = [item["text"] for item in telegram.edited]
    assert telegram.sent == []
    assert edited_texts[-1] == expected
    assert "Ужин: плов Вторник" not in edited_texts[-1]


def test_conversation_uses_structured_menu_render_after_plan_week_menu(
    monkeypatch, tmp_path: Path
):
    session = _bootstrap(monkeypatch, tmp_path, "conv_menu_plan_render.db")
    try:
        monkeypatch.setattr(
            "sreda.runtime.handlers._resolve_chat_feature_key",
            lambda _session, _tenant_id: "housewife_assistant",
        )

        glued_bad_final = (
            "Готово, меню составлено ✅ Пятница, 24 апреля:\n"
            "• Завтрак: блины\n"
            "• Обед: суп\n"
            "• Ужин: плов Суббота, 25 апреля:\n"
            "• Завтрак: творог\n"
            "• Обед: борщ\n"
            "• Ужин: рыба Собрать список покупок?"
        )
        scripted = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "plan_week_menu",
                        "args": {
                            "week_start": "2026-04-20",
                            "days": [
                                {
                                    "day_of_week": 4,
                                    "meals": {
                                        "breakfast": {"free_text": "блины"},
                                        "lunch": {"free_text": "суп"},
                                        "dinner": {"free_text": "плов"},
                                    },
                                },
                                {
                                    "day_of_week": 5,
                                    "meals": {
                                        "breakfast": {"free_text": "творог"},
                                        "lunch": {"free_text": "борщ"},
                                        "dinner": {"free_text": "рыба"},
                                    },
                                },
                            ],
                        },
                        "id": f"tc_{uuid4().hex[:8]}",
                    }
                ],
                additional_kwargs={"reasoning_content": "thinking trace"},
            ),
            AIMessage(content=glued_bad_final),
        ]
        telegram = FakeTelegram()

        from sreda.services.ack_progress import TelegramAckProgressController

        ack_progress = TelegramAckProgressController(
            telegram_client=telegram,
            chat_id="100000001",
            ack_message_id_future=555,
            enabled=True,
        )
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=StreamingFinalFakeLLM(scripted),
            embedding_client=ConstantEmbeddingClient(),
            ack_progress_controller=ack_progress,
        )
        queued = svc.enqueue_action(_chat_envelope("составь меню на выходные"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    expected = (
        "Меню на неделю 20–26 апреля:\n\n"
        "Пятница, 24 апреля\n"
        "• Завтрак: блины\n"
        "• Обед: суп\n"
        "• Ужин: плов\n\n"
        "Суббота, 25 апреля\n"
        "• Завтрак: творог\n"
        "• Обед: борщ\n"
        "• Ужин: рыба\n\n"
        "Собрать список покупок?"
    )
    edited_texts = [item["text"] for item in telegram.edited]
    assert telegram.sent == []
    assert edited_texts[-1] == expected
    assert "Ужин: плов Суббота" not in edited_texts[-1]


def test_conversation_menu_plan_render_allows_family_read_tool(
    monkeypatch, tmp_path: Path
):
    session = _bootstrap(monkeypatch, tmp_path, "conv_menu_plan_family_read.db")
    try:
        monkeypatch.setattr(
            "sreda.runtime.handlers._resolve_chat_feature_key",
            lambda _session, _tenant_id: "housewife_assistant",
        )

        scripted = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_family_members",
                        "args": {},
                        "id": f"tc_{uuid4().hex[:8]}",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "plan_week_menu",
                        "args": {
                            "week_start": "2026-04-20",
                            "days": [
                                {
                                    "day_of_week": 4,
                                    "meals": {
                                        "breakfast": {"free_text": "блины"},
                                        "lunch": {"free_text": "суп"},
                                        "dinner": {"free_text": "плов"},
                                    },
                                },
                            ],
                        },
                        "id": f"tc_{uuid4().hex[:8]}",
                    }
                ],
            ),
            AIMessage(
                content=(
                    "Готово ✅ Пятница, 24 апреля:\n"
                    "• Завтрак: блины\n"
                    "• Обед: суп\n"
                    "• Ужин: плов Собрать список покупок?"
                )
            ),
        ]
        telegram = FakeTelegram()

        from sreda.services.ack_progress import TelegramAckProgressController

        ack_progress = TelegramAckProgressController(
            telegram_client=telegram,
            chat_id="100000001",
            ack_message_id_future=555,
            enabled=True,
        )
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=StreamingFinalFakeLLM(scripted),
            embedding_client=ConstantEmbeddingClient(),
            ack_progress_controller=ack_progress,
        )
        queued = svc.enqueue_action(_chat_envelope("составь меню на пятницу"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    edited_texts = [item["text"] for item in telegram.edited]
    assert edited_texts[-1] == (
        "Меню на неделю 20–26 апреля:\n\n"
        "Пятница, 24 апреля\n"
        "• Завтрак: блины\n"
        "• Обед: суп\n"
        "• Ужин: плов\n\n"
        "Собрать список покупок?"
    )


def test_conversation_menu_plan_render_does_not_hide_other_mutations(
    monkeypatch, tmp_path: Path
):
    session = _bootstrap(monkeypatch, tmp_path, "conv_menu_plan_other_mutation.db")
    try:
        monkeypatch.setattr(
            "sreda.runtime.handlers._resolve_chat_feature_key",
            lambda _session, _tenant_id: "housewife_assistant",
        )

        final_text = "Готово: меню составлено, молоко добавила в покупки."
        scripted = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "plan_week_menu",
                        "args": {
                            "week_start": "2026-04-20",
                            "days": [
                                {
                                    "day_of_week": 4,
                                    "meals": {
                                        "breakfast": {"free_text": "блины"},
                                        "lunch": {"free_text": "суп"},
                                        "dinner": {"free_text": "плов"},
                                    },
                                },
                            ],
                        },
                        "id": f"tc_{uuid4().hex[:8]}",
                    },
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_shopping_items",
                        "args": {
                            "items": [
                                {"title": "молоко", "category": "молочные"}
                            ]
                        },
                        "id": f"tc_{uuid4().hex[:8]}",
                    },
                ],
            ),
            AIMessage(content=final_text),
        ]
        telegram = FakeTelegram()

        from sreda.services.ack_progress import TelegramAckProgressController

        ack_progress = TelegramAckProgressController(
            telegram_client=telegram,
            chat_id="100000001",
            ack_message_id_future=555,
            enabled=True,
        )
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=StreamingFinalFakeLLM(scripted),
            embedding_client=ConstantEmbeddingClient(),
            ack_progress_controller=ack_progress,
        )
        queued = svc.enqueue_action(
            _chat_envelope("составь меню на пятницу и добавь молоко")
        )
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    edited_texts = [item["text"] for item in telegram.edited]
    assert edited_texts[-1] == final_text
    assert "Меню на неделю 20–26 апреля" not in edited_texts[-1]


def test_menu_display_render_intent_stays_week_scope_only():
    from sreda.runtime.handlers import _is_menu_display_read_intent

    assert _is_menu_display_read_intent("покажи меню на неделю") is True
    assert _is_menu_display_read_intent("что на этой неделе?") is True
    assert _is_menu_display_read_intent("какое меню на следующую неделю") is True
    assert _is_menu_display_read_intent("что в меню на среду?") is False
    assert _is_menu_display_read_intent("составь меню на неделю") is False
    assert _is_menu_display_read_intent("покажи меню и собери список покупок") is False
    assert (
        _is_menu_display_read_intent(
            "покажи меню на неделю и предложи, что улучшить"
        )
        is False
    )
    assert (
        _is_menu_display_read_intent(
            "покажи меню на неделю и скажи, где есть рыба"
        )
        is False
    )
    assert _is_menu_display_read_intent("покажи меню на неделю без молока") is False


def test_conversation_streams_plain_final_answer_into_ack(
    monkeypatch, tmp_path: Path
):
    session = _bootstrap(monkeypatch, tmp_path, "conv_plain_stream.db")
    try:
        telegram = FakeTelegram()
        from sreda.services.ack_progress import TelegramAckProgressController

        ack_progress = TelegramAckProgressController(
            telegram_client=telegram,
            chat_id="100000001",
            ack_message_id_future=555,
            enabled=True,
        )
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=StreamingFinalFakeLLM([
                AIMessage(content="Привет, Борис! Чем могу помочь?"),
            ]),
            embedding_client=ConstantEmbeddingClient(),
            ack_progress_controller=ack_progress,
        )
        queued = svc.enqueue_action(_chat_envelope("Привет"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    edited_texts = [item["text"] for item in telegram.edited]
    assert telegram.sent == []
    assert any(
        text.startswith("Привет") and text != "Привет, Борис! Чем могу помочь?"
        for text in edited_texts
    )
    assert edited_texts[-1] == "Привет, Борис! Чем могу помочь?"
    assert edited_texts.count("Привет, Борис! Чем могу помочь?") == 1
    assert FINAL_PROGRESS_TEXT not in edited_texts


def test_conversation_max_waits_before_final_ack_edit_outbox(
    monkeypatch, tmp_path: Path
):
    class _AckController:
        enabled = True
        final_edit_planned = False

        def __init__(self) -> None:
            self.streamed: list[str] = []
            self.keep_visible_calls: list[str] = []
            self.current_text: str | None = None

        def schedule_progress(self, text=None):
            pass

        def schedule_almost_done(self):
            pass

        def schedule_stream_text(self, text, *, min_interval_seconds=0.8, force=False):
            self.streamed.append(text)
            self.current_text = text.strip()

        def has_stream_text(self):
            return self.current_text is not None

        def is_stream_text_current(self, text: str):
            return self.current_text == text.strip()

        async def flush_stream_final_text(self, text: str):
            self.schedule_stream_text(text, min_interval_seconds=0, force=True)

        async def drain(self):
            pass

        async def keep_stream_partial_visible(self, final_text: str):
            self.keep_visible_calls.append(final_text)

        async def ack_message_id(self, *, timeout_seconds: float = 2.0):
            return "max-ack-1"

        def mark_final_edit_planned(self):
            self.final_edit_planned = True

    session = _bootstrap(monkeypatch, tmp_path, "conv_max_stream.db")
    ack_progress = _AckController()
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
        svc = ActionRuntimeService(
            session,
            llm_client=StreamingFinalFakeLLM(scripted),
            embedding_client=ConstantEmbeddingClient(),
            ack_progress_controller=ack_progress,
        )
        queued = svc.enqueue_action(_max_chat_envelope("у меня дочь Маша 9 лет"))
        asyncio.run(svc.process_job(queued.job_id))

        outbox = session.query(OutboxMessage).one()
        payload = json.loads(outbox.payload_json)
    finally:
        session.close()

    assert ack_progress.streamed
    assert ack_progress.keep_visible_calls == ["Запомнил — дочь Маша, 9 лет."]
    assert ack_progress.final_edit_planned is True
    assert payload["_ack_edit_message_id"] == "max-ack-1"
    assert payload["_ack_final_already_visible"] is True


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
    system_content = _message_content_text(call_messages[0].content)
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
    assert "у меня дочь Маша 9 лет" in _message_content_text(call_msgs[0].content)

    # Access count should have been bumped by load_memories touch
    # (in the second invocation). Re-opening session to verify.
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
        by_name["recall_memory"].invoke({"query": "foo", "top_k": 3})

        assert r1.startswith("saved_core:")
        assert r2.startswith("saved_episode:")
        rows = session.query(AssistantMemory).all()
        contents = {row.content for row in rows}
        tiers = {row.content: row.tier for row in rows}
    finally:
        session.close()

    assert "live in Moscow" in contents
    assert "bad day" in contents
    assert tiers["live in Moscow"] == "core"
    assert tiers["bad day"] == "episodic"


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
    второй retry, а safety_net заменяет текст на честный fallback ack
    (задача #59) вместо «Готово.». `_hallucination_nudged` флаг не
    позволяет уйти в бесконечный retry-loop."""
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


def test_safety_net_sends_admin_alert_and_honest_ack(monkeypatch, tmp_path: Path):
    """Задача #59: когда safety_net срабатывает и called_tools пустой —
    (1) outbound text = честный fallback ack «Не получилось надёжно
        записать это. Повтори, пожалуйста?»;
    (2) send_admin_alert вызван с severity=P1 и гранулярным dedupe_key
        (5 частей: unbacked_claim : tenant : feature : 8-char-hash : date).

    Дополнительно closes Codex MAJOR R3 (datetime monkeypatch через
    модульный хелпер _utc_today_iso).
    """
    captured_alerts: list[dict] = []

    def fake_send(severity, title, body, *, dedupe_key=None, extra_context=None):
        captured_alerts.append({
            "severity": severity,
            "title": title,
            "body": body,
            "dedupe_key": dedupe_key,
            "extra_context": extra_context,
        })

    # Переопределяем autouse-фикстуру (последний setattr выигрывает в pytest)
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", fake_send,
    )
    # Замораживаем UTC-дату через модульный хелпер (R3 fix)
    monkeypatch.setattr(
        "sreda.runtime.handlers._utc_today_iso",
        lambda: "2026-05-22",
    )

    session = _bootstrap(monkeypatch, tmp_path, "safety_net_alert.db")
    try:
        scripted = [
            # iter 0: claim («Сохранила» + «в книгу» = recipe category)
            # без tool → detector fires в текущей конфигурации
            AIMessage(
                content="Сохранила рецепт борща в книгу.",
                tool_calls=[],
            ),
            # iter 1 (после nudge retry): ОПЯТЬ claim без tool →
            # RETRY_EXHAUSTED → safety_net + admin alert
            AIMessage(
                content="Готово! Записала рецепт борща в книгу.",
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
        envelope = _chat_envelope("сохрани рецепт борща")
        queued = svc.enqueue_action(envelope)
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    # Alert assertions — гранулярный dedupe_key
    assert len(captured_alerts) >= 1, "send_admin_alert не вызывался"
    alert = captured_alerts[0]
    assert alert["severity"] == "P1"
    assert "Unbacked claim" in alert["title"]
    # 5 частей через `:` — unbacked_claim : tenant : feature : hash : date
    parts = alert["dedupe_key"].split(":")
    assert len(parts) == 5, (
        f"ожидаем 5 частей dedupe_key, получили {parts}"
    )
    assert parts[0] == "unbacked_claim"
    assert parts[1] == "t1"  # tenant_id из _chat_envelope
    # parts[2] = feature_key (любой непустой — в тесте может быть test_chat_skill)
    assert parts[2], f"feature_key пуст в dedupe_key: {parts}"
    # parts[3] = 8-символьный hex hash
    assert len(parts[3]) == 8, f"hash должен быть 8 символов, got {parts[3]}"
    assert all(c in "0123456789abcdef" for c in parts[3])
    # parts[4] = замороженная дата (из monkeypatch _utc_today_iso)
    assert parts[4] == "2026-05-22"

    # Outbound: честный fallback ack, НЕ оригинальная ложь
    assert len(telegram.sent) == 1
    outbound_text = telegram.sent[0]["text"]
    assert "Сохранила рецепт" not in outbound_text, (
        f"оригинальная ложь утекла: {outbound_text!r}"
    )
    assert "Записала рецепт" not in outbound_text, (
        f"вторая итерация лжи утекла: {outbound_text!r}"
    )
    # Empty called_tools → честный fallback (R4 fix вместо «Готово.»)
    assert outbound_text == (
        "Не получилось надёжно записать это. Повтори, пожалуйста?"
    )


# ---------------------------------------------------------------------------
# PR-1 golden-regression gap tests (Sub-A12 Phase E)
# ---------------------------------------------------------------------------
# Characterise post-loop OUTPUT guards that the existing suite did not pin.
# These must stay green through the shared-spine refactor — finalize_chat_reply
# (seam 3) must reproduce them byte-for-byte. Time-dependent guards
# (greeting-strip) and WARNING-only guards (date-drift) are covered by their
# own helper unit tests, not re-pinned here (avoids flaky time-based handler
# tests).


def test_reply_with_buttons_renders_inline_keyboard(monkeypatch, tmp_path: Path):
    """LLM calls reply_with_buttons → outbound text = the tool's text and
    reply_markup carries an inline keyboard with callback_data btn_reply:<tok>.
    Pins the side-channel render (pending_buttons_state) — the highest-risk
    coupling for the finalize_chat_reply extraction."""
    import sreda.db.models.reply_buttons  # noqa: F401 — register table for _bootstrap

    session = _bootstrap(monkeypatch, tmp_path, "pr1_buttons.db")
    try:
        # Force housewife feature so build_housewife_tools (with the
        # reply_with_buttons tool + pending_buttons_state) is wired —
        # mirrors the menu-render tests above.
        monkeypatch.setattr(
            "sreda.runtime.handlers._resolve_chat_feature_key",
            lambda _session, _tenant_id: "housewife_assistant",
        )
        scripted = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "reply_with_buttons",
                        "args": {
                            "text": "Какой вариант выберешь?",
                            "buttons": ["Вариант А", "Вариант Б"],
                        },
                        "id": f"tc_{uuid4().hex[:8]}",
                    }
                ],
            ),
            AIMessage(content="(финальный текст LLM — должен быть перезаписан)"),
        ]
        fake_llm = FakeLLM(scripted)
        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=fake_llm,
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("предложи варианты"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert len(telegram.sent) == 1
    sent = telegram.sent[0]
    # tool text overrides whatever the LLM wrote in its final AI message
    assert sent["text"] == "Какой вариант выберешь?"
    markup = sent["reply_markup"]
    assert markup is not None, "reply_with_buttons must produce an inline keyboard"
    rows = markup["inline_keyboard"]
    labels = [btn["text"] for row in rows for btn in row]
    assert labels == ["Вариант А", "Вариант Б"]
    for row in rows:
        for btn in row:
            assert btn["callback_data"].startswith("btn_reply:")


def test_weather_hallucination_substituted_when_get_weather_not_called(
    monkeypatch, tmp_path: Path
):
    """User asks about weather, LLM fabricates a forecast WITHOUT calling
    get_weather → reply replaced with the weather-hallucination substitute.
    Pins the _is_weather_hallucination post-loop guard."""
    from sreda.runtime.handlers import _WEATHER_HALLUCINATION_SUBSTITUTE

    session = _bootstrap(monkeypatch, tmp_path, "pr1_weather.db")
    try:
        scripted = [
            AIMessage(
                content="Завтра в Москве +18°C днём, без осадков, ветер слабый.",
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
        queued = svc.enqueue_action(
            _chat_envelope("какая погода завтра в Москве")
        )
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert len(telegram.sent) == 1
    assert telegram.sent[0]["text"] == _WEATHER_HALLUCINATION_SUBSTITUTE


def test_provider_refusal_text_substituted(monkeypatch, tmp_path: Path):
    """LLM final reply is a provider safety-refusal string → replaced with the
    Russian refusal substitute. Pins the _is_provider_refusal /
    _is_predominantly_non_russian post-loop guard."""
    from sreda.runtime.handlers import _REFUSAL_SUBSTITUTE_MESSAGE

    session = _bootstrap(monkeypatch, tmp_path, "pr1_refusal.db")
    try:
        scripted = [
            AIMessage(
                content=(
                    "The request was rejected because it was considered "
                    "high risk."
                ),
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
        queued = svc.enqueue_action(_chat_envelope("привет"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert len(telegram.sent) == 1
    assert telegram.sent[0]["text"] == _REFUSAL_SUBSTITUTE_MESSAGE


# ---------------------------------------------------------------------------
# PR-1 loop-seam gap tests (Sub-A12 Phase E)
# ---------------------------------------------------------------------------
# Characterise two uncovered paths through _run_legacy_react_loop that the
# independent review flagged.  A NameError in a moved closure would only
# surface on these paths; pinning them here protects the seam extraction.


def test_primary_llm_raises_fallback_engages_and_replies(
    monkeypatch, tmp_path: Path
):
    """Primary invoke raises → fallback client engages and produces the reply.

    Mechanism: execute_conversation_chat reads context["_fallback_llm_client"]
    and calls .bind_tools(tools) on it if present (handlers.py:3236-3239).
    We inject a separate FakeLLM as the fallback via node_execute_action
    (the graph node that builds context before calling the handler).

    Primary _BoundFakeLLM is patched to raise LLMCallTimeout on invoke.
    Fallback FakeLLM returns a plain-text reply.
    Guard: the admin-alert call for "fallback engaged" is suppressed so the
    test doesn't fail on missing SREDA_ADMIN_* env vars.
    """
    from sreda.services.llm import LLMCallTimeout
    import sreda.runtime.graph as _graph_module

    session = _bootstrap(monkeypatch, tmp_path, "pr1_fallback.db")

    # Suppress admin alerts — the fallback path fires send_admin_alert
    # with severity P1; swallow it so the test is self-contained.
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert",
        lambda *_a, **_kw: None,
    )

    # Primary LLM: raises LLMCallTimeout on every invoke.
    class _RaisingBound:
        tools: list = []

        def invoke(self, messages):
            raise LLMCallTimeout("primary timed out (test)")

        def stream(self, messages):
            raise LLMCallTimeout("primary timed out (test)")

    class _RaisingLLM:
        def bind_tools(self, tools):
            b = _RaisingBound()
            b.tools = list(tools)
            return b

        def invoke(self, messages):
            raise LLMCallTimeout("primary timed out (test)")

    primary_llm = _RaisingLLM()

    # Fallback LLM: returns a plain reply immediately.
    fallback_llm = FakeLLM([
        AIMessage(content="Fallback ответил — первичный упал."),
    ])

    # Inject fallback into context by wrapping node_execute_action.
    _orig_node = _graph_module.node_execute_action

    async def _patched_node(state, config):
        config["configurable"]["_fallback_llm_client"] = fallback_llm.bind_tools([])
        return await _orig_node(state, config)

    monkeypatch.setattr(_graph_module, "node_execute_action", _patched_node)

    # But the handler checks context["_fallback_llm_client"] not
    # config["configurable"]["_fallback_llm_client"] — we need to also
    # patch node_execute_action to inject into the context dict it builds.
    # Simpler: patch execute_conversation_chat's context read directly by
    # wrapping node_execute_action to add the key to the context dict:
    async def _patched_node2(state, config):
        result = await _orig_node(state, config)
        return result

    # Actually the cleaner injection is to monkeypatch _graph_module so that
    # node_execute_action adds "_fallback_llm_client" to the context dict
    # BEFORE calling the handler.  Let's wrap it properly:
    async def _inject_fallback_node(state, config):
        # Build the context that the node would build, then inject our key.
        # We do this by temporarily patching the module-level dict lookup.
        import sreda.runtime.graph as _g
        orig = _g.node_execute_action
        # Wrap the context dict after it's built inside the node.
        # Since we can't easily intercept the dict mid-function, we patch
        # execute_conversation_chat in handlers to intercept its context arg.
        return await orig(state, config)

    # The cleanest approach: monkeypatch execute_conversation_chat to wrap
    # the context dict and insert _fallback_llm_client before it runs.
    import sreda.runtime.handlers as _handlers

    _orig_handler = _handlers.execute_conversation_chat

    async def _wrapped_handler(session_arg, action, context):
        context["_fallback_llm_client"] = fallback_llm
        return await _orig_handler(session_arg, action, context)

    monkeypatch.setattr(_handlers, "execute_conversation_chat", _wrapped_handler)
    # Also route the HANDLERS registry to our wrapper. Use monkeypatch.setitem
    # (auto-restored at teardown) rather than direct dict assignment + manual
    # finally-restore — avoids the module-state-leak trap
    # (feedback_pytest_monkeypatch_required).
    monkeypatch.setitem(_handlers.HANDLERS, "conversation.chat", _wrapped_handler)

    try:
        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=primary_llm,
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("привет"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert len(telegram.sent) == 1
    assert "Fallback ответил" in telegram.sent[0]["text"]


def test_turn_timeout_with_successful_tools_returns_summary(
    monkeypatch, tmp_path: Path
):
    """Turn timeout fires after iter=0 succeeds → reply is _format_timeout_summary.

    Mechanism (handlers.py:3523-3531):
        if _turn_timed_out:
            if successful_tool_counts:
                text = _format_timeout_summary(successful_tool_counts)
            else:
                text = "Не успел(а) обдумать..."

    We need successful_tool_counts to be non-empty when the timeout fires.
    The timeout check is at the TOP of each loop iteration.  So:
      - iter=0 check: elapsed must NOT exceed the cap → passes normally
      - iter=0 body: tool call (save_core_fact) executes → successful_tool_counts populated
      - iter=1 check: elapsed DOES exceed the cap → _turn_timed_out=True, break

    We achieve this by monkeypatching the monotonic clock so that
    _turn_start_monotonic = 0, first elapsed check returns 0 (iter=0 passes),
    second elapsed check returns a large value (iter=1 times out).
    """
    import time as _real_time
    from sreda.runtime.handlers import _format_timeout_summary

    session = _bootstrap(monkeypatch, tmp_path, "pr1_timeout_tools.db")

    # Script: iter=0 emits save_core_fact (succeeds); there is no iter=1 reply
    # because the timeout fires before the LLM is called again.
    scripted = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "save_core_fact",
                    "args": {"content": "люблю кофе по утрам"},
                    "id": f"tc_{uuid4().hex[:8]}",
                }
            ],
        ),
        # This response would be used only if the loop reaches iter=1, which
        # it must NOT (timeout fires at the iter=1 check). Having it here
        # guards against accidental use.
        AIMessage(content="(не должно быть отправлено — timeout должен сработать)"),
    ]

    # Strategy: replace the module-level ``time`` binding in
    # sreda.runtime.handlers with a controlled fake object so that only
    # the handler's own time.monotonic() calls are intercepted.  asyncio
    # and the executor-level asyncio.wait_for use their own time references
    # and are unaffected.
    #
    # Clock sequence for _run_legacy_react_loop:
    #   call 0 → 0.0   (_turn_start_monotonic = time.monotonic(), before loop)
    #   call 1 → 0.0   (iter=0 check: elapsed = 0.0 - 0.0 = 0.0; NOT > cap=0 → passes)
    #   call 2 → 999.0 (iter=1 check: elapsed = 999.0 - 0.0 = 999.0; > 0 → fires)
    # Additional calls return 999.0 (logging path inside the timeout branch).
    _mono_seq = [0.0, 0.0, 999.0]
    _mono_idx = [0]

    import types as _types
    import time as _real_time

    _fake_time = _types.SimpleNamespace(
        **{k: getattr(_real_time, k) for k in dir(_real_time) if not k.startswith("__")},
    )

    def _fake_monotonic() -> float:
        val = _mono_seq[min(_mono_idx[0], len(_mono_seq) - 1)]
        _mono_idx[0] += 1
        return val

    _fake_time.monotonic = _fake_monotonic

    import sreda.runtime.handlers as _h
    monkeypatch.setattr(_h, "time", _fake_time)
    monkeypatch.setattr(_h, "CHAT_TURN_TIMEOUT_SECONDS", 0)

    try:
        telegram = FakeTelegram()
        svc = ActionRuntimeService(
            session,
            telegram_client=telegram,
            llm_client=FakeLLM(scripted),
            embedding_client=ConstantEmbeddingClient(),
        )
        queued = svc.enqueue_action(_chat_envelope("запомни про кофе"))
        asyncio.run(svc.process_job(queued.job_id))
    finally:
        session.close()

    assert len(telegram.sent) == 1
    reply = telegram.sent[0]["text"]
    # The reply must be the timeout-summary path (not the generic
    # "не успел обдумать" and not a normal LLM reply).
    # _format_timeout_summary({"save_core_fact": 1}) →
    #   "Успела сделать (память). Открой Mini App..."
    expected_summary = _format_timeout_summary({"save_core_fact": 1})
    assert reply == expected_summary, (
        f"Expected timeout summary {expected_summary!r}, got {reply!r}"
    )
