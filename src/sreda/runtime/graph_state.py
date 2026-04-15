"""LangGraph state schema for the assistant action-execution graph.

The graph is the canonical runtime for per-action processing (Phase 1 of
the roadmap). It replaces the old hand-rolled ``_route_action`` switch in
``ActionRuntimeService`` with a compiled graph that supports:

  * declarative conditional routing (policy pass/fail, success/error);
  * thread-scoped checkpointing (for Phase 3 memory);
  * node-level observability for future tracing.

State fields are intentionally JSON-serializable (so the checkpointer can
snapshot them). Mutable side-effect handles (SQLAlchemy session, Telegram
client) are passed through ``RunnableConfig.configurable`` — they are not
part of the state.
"""

from __future__ import annotations

from typing import Any, TypedDict


class AssistantGraphState(TypedDict, total=False):
    # Input — set by the caller when invoking the graph.
    action: dict[str, Any]
    run_id: str
    job_id: str

    # Populated by ``load_context`` node.
    context: dict[str, Any]

    # Populated by ``load_profile`` node (Phase 2). May be absent when
    # ``action.user_id`` is ``None`` — policy/handlers treat that as a
    # "no profile" context rather than erroring.
    profile: dict[str, Any]
    skill_configs: list[dict[str, Any]]

    # Populated by ``execute_action`` node on success.
    replies: list[dict[str, Any]]  # [{"text": ..., "reply_markup": ...}]

    # Populated by ``policy_guard`` or ``execute_action`` on failure,
    # consumed by ``persist_error`` node.
    error_code: str
    error_message: str
    error_reply_markup: dict[str, Any] | None

    # Set by the terminal persist nodes so the caller can inspect outcome.
    outcome: str  # "completed" | "failed"
