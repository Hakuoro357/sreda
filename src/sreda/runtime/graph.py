"""The assistant action-execution graph (Phase 1 runtime).

Replaces the hand-rolled ``_route_action`` switch in
``ActionRuntimeService``. Built once at process start, invoked per job
through ``ActionRuntimeService.process_job``.

Flow::

    START → load_context → policy_guard ─ok→ execute_action ─ok→ persist_replies → END
                                │                   │
                                error              error
                                ▼                   ▼
                           persist_error ◄──────────┘
                                │
                                ▼
                               END

Session and Telegram client are non-serializable, so they travel in
``config.configurable`` (LangGraph's runtime config) rather than in the
state itself. State holds only JSON-safe things (serialized action,
context, replies, error). This keeps the graph checkpointable.

Checkpointer — for Phase 1 we use ``InMemorySaver``. The
``thread_id`` machinery is already plumbed through so swapping to a
persistent backend (PostgresSaver) in Phase 3 will be a one-line
change in ``_make_checkpointer``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from sreda.db.models import AgentRun
from sreda.db.models.core import Job, OutboxMessage, TenantFeature
from sreda.db.repositories.memory import MemoryRepository
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.delivery_policy import DeliveryKind, decide_delivery
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.graph_state import AssistantGraphState
from sreda.runtime.handlers import HANDLERS, ActionRuntimeError, RuntimeReply
from sreda.runtime.policy import evaluate_policy
from sreda.services import trace
from sreda.services.billing import BillingService
from sreda.services.embeddings import EmbeddingClient
from sreda.services.privacy_guard import get_default_privacy_guard


def _utcnow() -> datetime:
    return datetime.now(UTC)


def sanitize_error_message(message: str) -> str:
    """Route an error string through the shared privacy guard.

    Error messages can accidentally carry credentials embedded by
    upstream handlers. Both the persisted ``AgentRun.error_message_sanitized``
    column and the user-visible Telegram reply go through this so the
    DB never stores — and the user never sees — a raw secret."""
    result = get_default_privacy_guard().sanitize_text(message)
    if result is None:
        return ""
    return result.sanitized_text


def _session(config: dict) -> Session:
    return config["configurable"]["session"]


def _telegram(config: dict) -> TelegramClient | None:
    return config["configurable"].get("telegram_client")


def _action(state: AssistantGraphState) -> ActionEnvelope:
    return ActionEnvelope(**state["action"])


def _find_skill_config(
    skill_configs: list[dict[str, Any]], feature_key: str | None
) -> dict[str, Any] | None:
    if feature_key is None:
        return None
    for cfg in skill_configs:
        if cfg.get("feature_key") == feature_key:
            return cfg
    return None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def node_load_context(state: AssistantGraphState, config: RunnableConfig) -> dict:
    session = _session(config)
    action = _action(state)

    billing_summary = BillingService(session).get_summary(action.tenant_id)
    eds_monitor_enabled = (
        session.query(TenantFeature)
        .filter(
            TenantFeature.tenant_id == action.tenant_id,
            TenantFeature.feature_key == "eds_monitor",
            TenantFeature.enabled.is_(True),
        )
        .one_or_none()
        is not None
    )
    context = {
        "tenant_id": action.tenant_id,
        "workspace_id": action.workspace_id,
        "assistant_id": action.assistant_id,
        "eds_monitor_enabled": eds_monitor_enabled,
        "billing_summary": {
            "base_active": billing_summary.base_active,
            "allowed_count": billing_summary.allowed_count,
            "connected_count": billing_summary.connected_count,
            "free_count": billing_summary.free_count,
        },
    }
    return {"context": context}


def node_load_profile(state: AssistantGraphState, config: RunnableConfig) -> dict:
    """Load the user's ``TenantUserProfile`` + per-skill configs into state.

    Runs after ``load_context``. When ``action.user_id`` is None (e.g.
    anonymous ``help.show`` before onboarding) we skip the load entirely
    — downstream nodes must tolerate missing profile."""
    session = _session(config)
    action = _action(state)
    if not action.user_id:
        return {"profile": {}, "skill_configs": []}

    repo = UserProfileRepository(session)
    profile = repo.get_profile(action.tenant_id, action.user_id)
    configs = repo.list_skill_configs(action.tenant_id, action.user_id)

    profile_dict: dict[str, Any]
    if profile is None:
        profile_dict = {}
    else:
        profile_dict = {
            "display_name": profile.display_name,
            "timezone": profile.timezone,
            "quiet_hours": UserProfileRepository.decode_quiet_hours(profile),
            "communication_style": profile.communication_style,
            "interest_tags": UserProfileRepository.decode_interest_tags(profile),
            # 2026-04-27: ты/вы выбираются на шаге 2 онбординга
            # (services.telegram_bot._handle_address_form_callback).
            # NULL = ещё не выбрано → LLM использует нейтральные формы.
            "address_form": profile.address_form,
        }

    skill_config_dicts = [
        {
            "feature_key": c.feature_key,
            "notification_priority": c.notification_priority,
            "token_budget_daily": c.token_budget_daily,
            "skill_params": UserProfileRepository.decode_skill_params(c),
        }
        for c in configs
    ]
    return {"profile": profile_dict, "skill_configs": skill_config_dicts}


def node_load_memories(state: AssistantGraphState, config: RunnableConfig) -> dict:
    """Retrieve top-k relevant memories for conversational actions.

    Only fires for ``conversation.chat`` — deterministic commands
    (``help.show``, ``/profile``, etc.) don't benefit from semantic
    memory recall and we want to keep their latency flat.

    Embedding client is resolved via ``config.configurable`` (tests
    inject fakes) or the settings factory (prod). If embeddings are
    disabled, we skip recall silently — the conversation handler will
    still work, just without any prior context.
    """
    action = _action(state)
    if action.action_type != "conversation.chat":
        return {"memories": []}

    query_text = str(action.params.get("text") or "").strip()
    if not query_text or not action.user_id:
        return {"memories": []}

    session = _session(config)
    embedding_client: EmbeddingClient | None = config["configurable"].get(
        "embedding_client"
    )
    if embedding_client is None:
        # Factory fallback — allow_fake=True keeps dev ergonomics when
        # LM Studio isn't running; prod should always configure a real
        # embeddings endpoint.
        from sreda.services.embeddings import get_embeddings_client

        embedding_client = get_embeddings_client(allow_fake=True)

    try:
        query_vec = embedding_client.embed_query(query_text)
    except Exception:
        # Embeddings down → skip, don't block the conversation.
        return {"memories": []}

    repo = MemoryRepository(session)
    hits = repo.recall(
        action.tenant_id, action.user_id, query_vec, top_k=10, min_score=0.1
    )
    # Touch access counts for returned memories — useful signal for
    # future recency boosts and eviction policies.
    for hit in hits:
        repo.touch_accessed(hit.memory.id)

    return {
        "memories": [
            {
                "id": hit.memory.id,
                "tier": hit.memory.tier,
                "content": hit.memory.content,
                "score": round(hit.score, 4),
                "source": hit.memory.source,
            }
            for hit in hits
        ]
    }


def node_policy_guard(state: AssistantGraphState, config: RunnableConfig) -> dict:
    action = _action(state)
    context = state.get("context") or {}
    error = evaluate_policy(action, context)
    if error is None:
        return {}
    return {
        "error_code": error.code,
        "error_message": error.message,
        "error_reply_markup": error.reply_markup,
    }


def node_execute_action(state: AssistantGraphState, config: RunnableConfig) -> dict:
    session = _session(config)
    action = _action(state)
    # Pass state-level snapshots down to the handler via ``context`` so
    # handlers don't need to peek at LangGraph state directly. Underscore
    # prefix signals "internal wiring, not user-visible data". Handlers
    # that care (profile.show, conversation.chat) read these keys; those
    # that don't simply ignore them.
    context = dict(state.get("context") or {})
    context["_profile"] = state.get("profile") or {}
    context["_skill_configs"] = state.get("skill_configs") or []
    context["_memories"] = state.get("memories") or []
    context["_llm_client"] = config["configurable"].get("llm_client")
    context["_embedding_client"] = config["configurable"].get("embedding_client")
    # Phase 4.5: run_id flows into context so the conversation handler
    # can attribute LLM usage to this specific AgentRun in
    # skill_ai_executions.
    context["_run_id"] = state.get("run_id")

    handler = HANDLERS.get(action.action_type)
    if handler is None:
        return {
            "error_code": "runtime_unsupported_action",
            "error_message": "Это действие пока не поддерживается.",
            "error_reply_markup": None,
        }

    try:
        replies = handler(session, action, context)
    except ActionRuntimeError as exc:
        return {
            "error_code": exc.code,
            "error_message": exc.message,
            "error_reply_markup": exc.reply_markup,
        }

    return {
        "replies": [
            {
                "text": reply.text,
                "reply_markup": reply.reply_markup,
                "feature_key": reply.feature_key,
            }
            for reply in replies
        ]
    }


async def node_persist_replies(state: AssistantGraphState, config: RunnableConfig) -> dict:
    session = _session(config)
    telegram = _telegram(config)
    action = _action(state)
    run = session.get(AgentRun, state["run_id"])
    job = session.get(Job, state["job_id"])
    context = state.get("context") or {}
    replies = state.get("replies") or []
    profile = state.get("profile") or {}
    skill_configs = state.get("skill_configs") or []
    is_interactive = action.inbound_message_id is not None
    now_utc = _utcnow()

    outbox_items: list[OutboxMessage] = []
    # Only the FIRST outbox row carries the trace; 2nd+ replies in the
    # same turn (rare for conversation.chat) reuse the same trace_id via
    # the shared ContextVar but don't re-embed the buffer. Worker still
    # emits on the first row's delivery — that's the one the user sees
    # first anyway.
    _trace_ctx = trace.current()
    _trace_stashed = False
    for reply in replies:
        feature_key = reply.get("feature_key")
        skill_config = _find_skill_config(skill_configs, feature_key)
        decision = decide_delivery(
            profile=profile,
            skill_config=skill_config,
            feature_key=feature_key,
            is_interactive=is_interactive,
            now_utc=now_utc,
        )

        payload: dict[str, Any] = {
            "chat_id": action.external_chat_id,
            "text": reply["text"],
            "reply_markup": reply["reply_markup"],
        }
        if _trace_ctx is not None and not _trace_stashed:
            payload["_trace"] = trace.serialize_for_outbox(_trace_ctx)
            _trace_stashed = True

        outbox = OutboxMessage(
            id=f"out_{uuid4().hex[:24]}",
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            user_id=action.user_id,
            channel_type="telegram",
            feature_key=feature_key,
            is_interactive=is_interactive,
            status="pending",
            scheduled_at=decision.defer_until_utc,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        session.add(outbox)
        session.flush()

        trace.record(
            "outbox.enqueued",
            chars=len(reply["text"] or ""),
            feature_key=feature_key,
            decision=decision.kind.value,
        )

        if decision.kind == DeliveryKind.drop:
            outbox.status = "muted"
        elif decision.kind == DeliveryKind.defer:
            # status stays 'pending' with scheduled_at set; the
            # OutboxDeliveryWorker picks it up at its exit time.
            pass
        else:  # send
            if telegram is not None:
                try:
                    await telegram.send_message(
                        chat_id=action.external_chat_id,
                        text=reply["text"],
                        reply_markup=reply["reply_markup"],
                    )
                    outbox.status = "sent"
                except TelegramDeliveryError:
                    # Leave pending; delivery worker will retry.
                    outbox.status = "pending"
            else:
                outbox.status = "sent"
        outbox_items.append(outbox)

    # Trace emission policy: delivery worker handles anything left in
    # 'pending' (defer / retry). Rows that will NOT reach the worker
    # (inline-sent, muted, missing telegram client) must be finalised
    # here so the trace isn't lost.
    if _trace_ctx is not None and outbox_items:
        first = outbox_items[0]
        if first.status == "sent":
            trace.emit_block(
                _trace_ctx,
                final_event_name="outbox.delivered",
                final_meta={
                    "chat": action.external_chat_id,
                    "status": "ok",
                    "path": "inline",
                },
            )
        elif first.status == "muted":
            trace.emit_block(
                _trace_ctx,
                final_event_name="outbox.muted",
                final_meta={"status": "muted"},
            )

    now = _utcnow()
    job.status = "completed"
    run.status = "completed"
    run.context_json = json.dumps(context, ensure_ascii=False)
    run.result_json = json.dumps(
        {
            "outbox_message_ids": [item.id for item in outbox_items],
            "outbox_statuses": [item.status for item in outbox_items],
            "reply_count": len(outbox_items),
        },
        ensure_ascii=False,
    )
    run.finished_at = now
    session.commit()
    return {"outcome": "completed"}


async def node_persist_error(state: AssistantGraphState, config: RunnableConfig) -> dict:
    session = _session(config)
    telegram = _telegram(config)
    action = _action(state)
    run = session.get(AgentRun, state["run_id"])
    job = session.get(Job, state["job_id"])
    context = state.get("context")

    error_code = state.get("error_code") or "runtime_unexpected_error"
    raw_message = state.get("error_message") or ""
    reply_markup = state.get("error_reply_markup")
    sanitized_message = sanitize_error_message(raw_message)

    outbox_ids: list[str] = []
    outbox_statuses: list[str] = []

    # Error replies are always interactive (they're responses to the
    # user's own command that failed) — bypass quiet-hours/mute policy
    # and deliver inline.
    _trace_ctx = trace.current()
    error_payload: dict[str, Any] = {
        "chat_id": action.external_chat_id,
        "text": sanitized_message,
        "reply_markup": reply_markup,
    }
    if _trace_ctx is not None:
        error_payload["_trace"] = trace.serialize_for_outbox(_trace_ctx)

    outbox = OutboxMessage(
        id=f"out_{uuid4().hex[:24]}",
        tenant_id=run.tenant_id,
        workspace_id=run.workspace_id,
        user_id=action.user_id,
        channel_type="telegram",
        is_interactive=action.inbound_message_id is not None,
        status="pending",
        payload_json=json.dumps(error_payload, ensure_ascii=False),
    )
    session.add(outbox)
    session.flush()

    trace.record(
        "outbox.enqueued",
        chars=len(sanitized_message or ""),
        error_code=error_code,
    )

    if telegram is not None:
        try:
            await telegram.send_message(
                chat_id=action.external_chat_id,
                text=sanitized_message,
                reply_markup=reply_markup,
            )
            outbox.status = "sent"
        except TelegramDeliveryError:
            outbox.status = "pending"
    outbox_ids.append(outbox.id)
    outbox_statuses.append(outbox.status)

    # If delivered inline, emit the trace here (worker won't reprocess
    # a 'sent' row). If still 'pending' — worker will emit on retry.
    if _trace_ctx is not None and outbox.status == "sent":
        trace.emit_block(
            _trace_ctx,
            final_event_name="outbox.delivered",
            final_meta={
                "chat": action.external_chat_id,
                "status": "error",
                "error_code": error_code,
                "path": "inline",
            },
        )

    now = _utcnow()
    job.status = "failed"
    run.status = "failed"
    if context is not None:
        run.context_json = json.dumps(context, ensure_ascii=False)
    run.result_json = json.dumps(
        {
            "outbox_message_ids": outbox_ids,
            "outbox_statuses": outbox_statuses,
            "reply_count": len(outbox_ids),
        },
        ensure_ascii=False,
    )
    run.error_code = error_code
    run.error_message_sanitized = sanitized_message
    run.finished_at = now
    session.commit()
    return {"outcome": "failed"}


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------


def _branch_after_policy(state: AssistantGraphState) -> str:
    return "error" if state.get("error_code") else "execute"


def _branch_after_execute(state: AssistantGraphState) -> str:
    return "error" if state.get("error_code") else "persist"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def _make_checkpointer() -> Any:
    """Phase 1: in-memory checkpointer. Phase 3 will swap to PostgresSaver.

    Kept as a function so the swap is a one-line change and tests can
    monkeypatch it if they need a fresh saver per test."""
    return InMemorySaver()


def build_assistant_graph(*, checkpointer: Any | None = None):
    graph = StateGraph(AssistantGraphState)
    graph.add_node("load_context", node_load_context)
    graph.add_node("load_profile", node_load_profile)
    graph.add_node("load_memories", node_load_memories)
    graph.add_node("policy_guard", node_policy_guard)
    graph.add_node("execute_action", node_execute_action)
    graph.add_node("persist_replies", node_persist_replies)
    graph.add_node("persist_error", node_persist_error)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "load_profile")
    graph.add_edge("load_profile", "load_memories")
    graph.add_edge("load_memories", "policy_guard")
    graph.add_conditional_edges(
        "policy_guard",
        _branch_after_policy,
        {"execute": "execute_action", "error": "persist_error"},
    )
    graph.add_conditional_edges(
        "execute_action",
        _branch_after_execute,
        {"persist": "persist_replies", "error": "persist_error"},
    )
    graph.add_edge("persist_replies", END)
    graph.add_edge("persist_error", END)

    return graph.compile(checkpointer=checkpointer or _make_checkpointer())


@lru_cache
def get_assistant_graph():
    """Process-wide compiled graph. Cached so we don't rebuild per request."""
    return build_assistant_graph()
