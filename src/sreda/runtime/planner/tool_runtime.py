"""Per-step idempotency context for durable-write tools (Sub-A12 Phase E).

Three public surfaces:

  ToolRuntimeContext   — frozen dataclass carrying the per-step execution
                         context; passed to every durable tool so it can call
                         emit_event() on its own session and dedup via
                         operation_id.

  current_tool_runtime() — returns the context stored in the current
                           ContextVar, or None when not running under the
                           planner executor (legacy / test path).

  bind_tool_runtime(ctx) — context manager used by the executor to set the
                           ContextVar around each tool invocation.

  allocate_operation_id() — pure, deterministic function; produces the same
                            id for the same (turn_key, step_id, tool_name) so
                            a sequential crash-retry re-derives the same key
                            and re-attaches to the existing ledger row.

  is_retriable(exc)       — conservative classifier for whether a tool
                            failure MAY be safely retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass

import sqlalchemy.exc


@dataclass(frozen=True, slots=True)
class ToolRuntimeContext:
    """Carries the per-step idempotency context to durable-write tools.

    Fields
    ------
    operation_id   — pre-allocated, globally-unique idempotency key for this
                     step; tools pass it to emit_event() and to ledger helpers.
    execution_id   — planner_executions.id for the running execution.
    step_id        — node id within the plan (stable across retries).
    tool_name      — name of the tool being invoked (mirrors ledger.tool).
    tenant_id      — needed by emit_event() for scoping.
    user_id        — optional; None when the execution is system-initiated.
    turn_key       — optional stable key for the conversation turn; used as
                     the primary input to allocate_operation_id().
    channel        — #163 Фаза 3d: канал хода (max/telegram/react) для react-аудита caused_by.
    thread_id      — #163 Фаза 3d: id диалоговой ветки для каузальной связки аудита.
    """

    operation_id: str
    execution_id: str
    step_id: str
    tool_name: str
    tenant_id: str
    user_id: str | None = None
    turn_key: str | None = None
    # #163 Фаза 3d: провенанс react-аудита. Defaults None — backcompat (plan-execute executor и
    # тесты, что не задают их). Заполняет react_loop.run_tools.
    channel: str | None = None
    thread_id: str | None = None


# Module-level ContextVar.  Default is None so that tools running outside
# the planner executor (legacy code path, unit tests that don't set it) can
# detect the absence of idempotency context via current_tool_runtime().
_TOOL_RUNTIME: contextvars.ContextVar[ToolRuntimeContext | None] = (
    contextvars.ContextVar("_TOOL_RUNTIME", default=None)
)


def current_tool_runtime() -> ToolRuntimeContext | None:
    """Return the ToolRuntimeContext for the currently-running tool, or None.

    Tools MUST treat None as "no idempotency context — behave as today"
    (i.e. fire-and-forget semantics, no ledger writes, no dedup).  This
    preserves backwards compatibility with every legacy code path that
    invokes a tool without going through the planner executor.
    """
    return _TOOL_RUNTIME.get()


@contextlib.contextmanager
def bind_tool_runtime(ctx: ToolRuntimeContext) -> Iterator[None]:
    """Set the ContextVar to *ctx* for the duration of the ``with`` block.

    The executor calls this around each tool invocation::

        with bind_tool_runtime(ctx):
            result = tool.invoke(...)

    For sync tools wrapped in ``asyncio.to_thread``, propagation works
    automatically: ``to_thread`` runs the callable via
    ``contextvars.copy_context().run(...)``, so the var set on the
    event-loop side is visible inside the thread — no extra wiring needed.

    The token is stored and reset in a ``finally`` clause so the var is
    always restored even if the tool raises.
    """
    token = _TOOL_RUNTIME.set(ctx)
    try:
        yield
    finally:
        _TOOL_RUNTIME.reset(token)


def allocate_operation_id(
    *,
    turn_key: str,
    step_id: str,
    tool_name: str,
) -> str:
    """Derive a stable, globally-unique operation id for one tool step.

    The function is **pure** — no uuid, no clock.  The same
    (turn_key, step_id, tool_name) triple always produces the same id, which
    is the crash-retry guarantee: a sequential retry of the same turn
    re-derives the same operation_id and re-attaches to the existing ledger
    row instead of inserting a duplicate.

    Hash: SHA-1 over fields joined with ``\\x1f`` (ASCII Unit Separator)
    — mirrors the convention in ``sreda.services.operation_id``.
    Result length is 43 chars (``op_`` + 40-hex-char SHA-1 digest), well
    within the String(64) keyspace used by StepExecutionLedger.operation_id.

    FAIL-CLOSED on a missing identity (Codex A/B #8b-1 R1, both MAJOR):
    ``turn_key`` is part of the stable PII-free identity and MUST be present
    and non-empty.  Collapsing a missing ``turn_key`` to ``""`` would make
    ids global only by ``(step_id, tool_name)``, so two unrelated executions
    that happen to share a step id + tool would derive the SAME id and
    collide on the globally-unique ``step_execution_ledger.operation_id`` /
    ``audit_outbox.operation_id`` keyspace.  The caller (executor) must
    supply the execution's ``turn_key``; a system-initiated turn must mint an
    explicit stable key rather than relying on a blank default.
    """
    if not turn_key:
        raise ValueError(
            "allocate_operation_id requires a non-empty turn_key — it is part "
            "of the globally-unique idempotency identity. A blank turn_key "
            "would collide across unrelated executions sharing (step_id, "
            "tool_name). System turns must mint an explicit stable key."
        )
    if not step_id or not tool_name:
        raise ValueError(
            "allocate_operation_id requires non-empty step_id and tool_name."
        )
    sep = "\x1f"
    payload = sep.join([turn_key, step_id, tool_name])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"op_{digest}"


def is_retriable(exc: BaseException, *, is_durable_write: bool) -> bool:
    """Return True iff *exc* is safe to retry for a tool with the given effect.

    EFFECT-AWARE + FAIL-CLOSED (Codex A/B #8b-1 R1, both MAJOR). A blanket
    exception allowlist is NOT safe: for a durable-write tool, a timeout /
    connection loss is an AMBIGUOUS post-invoke outcome — the write may have
    committed before the failure surfaced (e.g. ``asyncio.to_thread`` cannot
    cancel a running sync write; the thread can commit late). The accepted
    plan reserves such cases for recovery via the 'unknown' / 'unknown_pending'
    ledger statuses + probe, NEVER a blind retry that could double-write.

    Contract:
      - ``is_durable_write=True``  → ALWAYS False. No durable-write failure is
        ever blind-retriable from here; the recovery scanner must probe the
        actual state (audit_outbox) before deciding.
      - ``is_durable_write=False`` (read / request-local) → True ONLY for
        clearly-transient infra errors, since a read has no side effect to
        leave half-done:
          * asyncio.TimeoutError / builtins.TimeoutError (unified in 3.11+)
          * ConnectionError (BrokenPipeError, ConnectionResetError, …)
          * sqlalchemy OperationalError / DBAPIError with
            .connection_invalidated truthy
        Everything else (ValueError, ValidationError, TypeError, generic
        Exception) → False.

    ``is_durable_write`` is a REQUIRED keyword so every caller must consciously
    classify the tool — there is no silently-unsafe default.
    """
    if is_durable_write:
        return False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, (sqlalchemy.exc.OperationalError, sqlalchemy.exc.DBAPIError)):
        # connection_invalidated is set by SQLAlchemy when the underlying
        # DBAPI connection was detected as invalid (e.g. server dropped it).
        # Guard with getattr in case a subclass doesn't declare the attr.
        return bool(getattr(exc, "connection_invalidated", False))
    return False


__all__ = [
    "ToolRuntimeContext",
    "current_tool_runtime",
    "bind_tool_runtime",
    "allocate_operation_id",
    "is_retriable",
]
