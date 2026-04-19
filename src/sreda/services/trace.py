"""End-to-end timing trace for user conversation turns.

One turn = one multi-line block in ``sreda.trace`` logger, covering:
  webhook.received → voice.transcribe (optional) → llm.iter.N (tool loop)
  → outbox.enqueued → outbox.delivered

Why this exists: ``uvicorn.access`` / ``sreda.llm`` / ``sreda.feature_requests``
loggers each cover one slice of a turn. When a user complains ("didn't
reply", "took forever"), correlating across three files by timestamp is
painful. A single block per turn, keyed by a trace_id, makes post-hoc
analysis glance-readable.

Architecture — two-process handoff:

  uvicorn process buffers events in a ``TraceContext`` held in a
  ``ContextVar`` (propagates through asyncio.Task boundaries). When the
  chat handler finishes and the outbox row is about to be written, we
  serialise the buffer into ``OutboxMessage.payload_json["_trace"]``.
  The delivery worker (separate process) later deserialises, appends
  its own ``outbox.delivered`` event, and emits the final block via
  ``sreda.trace.info(...)``. Net effect: one block per turn, written
  ~1s after the user saw the bot reply. That lag is fine — this log
  is for debugging, not live monitoring.

Error path (handler crashed before outbox write): the top-level error
handler in the conversation flow calls ``emit_block`` directly with a
``final_event_name="trace.error"`` marker. That way every turn leaves
a record, even the failed ones.

Scope notes:
  * Meta only. No user text, no LLM response text — just counts,
    tokens, tool names, statuses. Changing this is easy (per-step
    meta is a free-form dict) but intentionally conservative now.
  * User-initiated turns only. Reminder firings / EDS notifications
    / callback buttons are NOT traced in v1. When we add them, the
    entry is the same: ``start_trace(...)`` at the top of the worker
    handler, ``emit_block(...)`` at its end.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger("sreda.trace")


@dataclass(slots=True)
class TraceEvent:
    """One step in a turn. ``at_ms`` is offset from turn start, NOT wall
    clock — makes sequential reading of the block trivial. ``meta`` is
    the free-form bag of per-step structured fields (tokens, tool names,
    content-type, etc.). Serialisable via plain ``asdict`` / kwarg round
    trip — keep types JSON-safe.
    """

    at_ms: int
    step: str
    duration_ms: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TraceContext:
    """Per-turn trace buffer. One instance per user conversation turn,
    passed implicitly via ``_current_trace`` ContextVar. ``events`` is
    appended to by every ``step(...)`` context manager. ``emit_block``
    flips ``_emitted`` so double-calls (e.g. from both the handler's
    error path and a late delivery worker) produce only one line."""

    trace_id: str
    user_id: str | None
    tenant_id: str | None
    channel: str
    started_at: datetime
    started_monotonic: float
    events: list[TraceEvent] = field(default_factory=list)
    _emitted: bool = False


_current_trace: ContextVar[TraceContext | None] = ContextVar(
    "sreda_trace", default=None
)


# ---------------------------------------------------------------------------
# Public API — called from instrumented sites
# ---------------------------------------------------------------------------


def start_trace(
    *,
    trace_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    channel: str = "telegram",
) -> TraceContext:
    """Begin a new trace for the current async-task scope.

    Overwrites any existing context in the same scope — callers that
    nest traces get the innermost one. For the conversation flow, the
    webhook entry is the only place this is called; everything below
    reads via ``current()``.

    If ``trace_id`` is None, a fresh ``trace_xxxx`` id is minted.
    Callers that want to align with ``AgentRun.id`` should pass it
    explicitly — but that row may not exist yet at entry time, so the
    default is to generate now.
    """
    ctx = TraceContext(
        trace_id=trace_id or f"trace_{uuid4().hex[:16]}",
        user_id=user_id,
        tenant_id=tenant_id,
        channel=channel,
        started_at=datetime.now(UTC),
        started_monotonic=time.monotonic(),
    )
    _current_trace.set(ctx)
    return ctx


def current() -> TraceContext | None:
    """Return the trace context for the current task, or None if none
    is active. Instrumented sites MUST tolerate ``None`` — unit tests
    and non-user code paths won't start traces."""
    return _current_trace.get()


def set_current(ctx: TraceContext | None) -> None:
    """Bind an arbitrary context to the current task. Used by the
    delivery worker after it deserialises a trace from ``payload_json``
    so the final ``outbox.delivered`` event is recorded into the same
    buffer before emission."""
    _current_trace.set(ctx)


@contextmanager
def step(name: str, **meta: Any):
    """Measure ``name`` duration into the current trace.

    Usage:
        with trace.step("voice.transcribe", provider="yandex") as m:
            audio = download()
            m["bytes_in"] = len(audio)
            text = recognise(audio)
            m["chars_out"] = len(text)

    The yielded dict is the SAME ``meta`` that the event will be
    emitted with — mutate it at any time inside the ``with`` block.
    If no trace is active, this is a no-op (the block runs, nothing
    gets recorded). That lets instrumented code run fine under tests
    or worker paths that don't bother starting traces.
    """
    ctx = current()
    if ctx is None:
        # No trace active — yield a throwaway dict so the caller's
        # ``m["key"] = value`` assignments don't crash.
        yield {}
        return

    started_rel_ms = int((time.monotonic() - ctx.started_monotonic) * 1000)
    t0 = time.monotonic()
    meta_dict: dict[str, Any] = dict(meta)
    try:
        yield meta_dict
    finally:
        duration_ms = int((time.monotonic() - t0) * 1000)
        ctx.events.append(
            TraceEvent(
                at_ms=started_rel_ms,
                step=name,
                duration_ms=duration_ms,
                meta=meta_dict,
            )
        )


def record(name: str, duration_ms: int = 0, **meta: Any) -> None:
    """Append a zero-duration or pre-measured event.

    Used where the duration is already known (e.g. the ``webhook.received``
    marker at turn start) or where wrapping a whole code block in
    ``step()`` would be awkward. No-op if no trace is active.
    """
    ctx = current()
    if ctx is None:
        return
    started_rel_ms = int((time.monotonic() - ctx.started_monotonic) * 1000)
    ctx.events.append(
        TraceEvent(
            at_ms=started_rel_ms,
            step=name,
            duration_ms=duration_ms,
            meta=dict(meta),
        )
    )


def serialize_for_outbox(ctx: TraceContext) -> dict[str, Any]:
    """Freeze the trace for stashing in ``OutboxMessage.payload_json``.

    Worker will deserialise it back with ``deserialize_from_outbox``.
    We keep this format stable — change with care, old outbox rows
    may still carry the previous shape. Fields are plain JSON types.
    """
    return {
        "trace_id": ctx.trace_id,
        "user_id": ctx.user_id,
        "tenant_id": ctx.tenant_id,
        "channel": ctx.channel,
        "started_at": ctx.started_at.isoformat(),
        "started_monotonic": ctx.started_monotonic,
        "events": [
            {
                "at_ms": e.at_ms,
                "step": e.step,
                "duration_ms": e.duration_ms,
                "meta": e.meta,
            }
            for e in ctx.events
        ],
    }


def deserialize_from_outbox(payload_trace: dict[str, Any]) -> TraceContext:
    """Reverse of ``serialize_for_outbox``. The worker calls this to
    rebuild the buffer, append one final event, and emit the block."""
    ctx = TraceContext(
        trace_id=str(payload_trace.get("trace_id") or f"trace_{uuid4().hex[:16]}"),
        user_id=payload_trace.get("user_id"),
        tenant_id=payload_trace.get("tenant_id"),
        channel=str(payload_trace.get("channel") or "telegram"),
        started_at=_parse_iso_utc(payload_trace.get("started_at")),
        # Monotonic clocks are per-process — once crossed, we can't resume
        # them. Anchor at 0 so any new events written to this context in
        # the worker land AFTER the last event (which still has its
        # original at_ms from the uvicorn side).
        started_monotonic=time.monotonic(),
    )
    for raw in payload_trace.get("events") or []:
        ctx.events.append(
            TraceEvent(
                at_ms=int(raw.get("at_ms", 0)),
                step=str(raw.get("step", "?")),
                duration_ms=int(raw.get("duration_ms", 0)),
                meta=dict(raw.get("meta") or {}),
            )
        )
    return ctx


def _parse_iso_utc(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def emit_block(
    ctx: TraceContext,
    *,
    final_event_name: str | None = None,
    final_meta: dict[str, Any] | None = None,
    final_at_ms: int | None = None,
) -> None:
    """Render the accumulated events as a multi-line block and log it.

    Idempotent: if this trace has already been emitted (e.g. by the
    handler's error path AND by the delivery worker), the second call
    is a no-op. This matters because cross-process paths can't easily
    coordinate "who finalises".

    ``final_event_name`` / ``final_meta`` let the caller append a
    final event atomically with emission — used by the delivery worker
    to tack on ``outbox.delivered`` without a second buffer hop.
    ``final_at_ms`` overrides the offset (the delivery worker's monotonic
    clock doesn't match the uvicorn process; if it has a better ms
    number it can supply it).
    """
    if ctx._emitted:
        return
    ctx._emitted = True

    if final_event_name:
        at_ms = (
            final_at_ms
            if final_at_ms is not None
            else int((time.monotonic() - ctx.started_monotonic) * 1000)
        )
        ctx.events.append(
            TraceEvent(
                at_ms=at_ms,
                step=final_event_name,
                duration_ms=0,
                meta=dict(final_meta or {}),
            )
        )

    lines: list[str] = []
    header = (
        f"=== TRACE {ctx.trace_id} "
        f"{ctx.started_at.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
        f"user={ctx.user_id or '-'} "
        f"chan={ctx.channel} ==="
    )
    lines.append(header)
    for event in ctx.events:
        meta_bits = _format_meta(event.meta)
        duration_suffix = (
            f" [{event.duration_ms}ms]" if event.duration_ms > 0 else ""
        )
        lines.append(
            f"  {event.at_ms:>5}ms  {event.step:<22}{duration_suffix} {meta_bits}".rstrip()
        )

    # Aggregates — the one-line summary readers scan first.
    total_ms = max((e.at_ms + e.duration_ms for e in ctx.events), default=0)
    llm_events = [e for e in ctx.events if e.step.startswith("llm.iter")]
    in_tok = sum(int(e.meta.get("in_tok", 0) or 0) for e in llm_events)
    out_tok = sum(int(e.meta.get("out_tok", 0) or 0) for e in llm_events)
    summary = (
        f"------- TOTAL {total_ms}ms "
        f"iters={len(llm_events)} "
        f"tok_in={in_tok} tok_out={out_tok}"
    )
    lines.append(summary)

    # One logger.info call with embedded newlines — keeps the whole
    # block atomic in the log file (no risk of interleaving with other
    # lines in between).
    logger.info("\n" + "\n".join(lines))


def _format_meta(meta: dict[str, Any]) -> str:
    """Render key=value pairs compact, stable key order. Strings with
    spaces get quoted to keep the log grep-friendly."""
    if not meta:
        return ""
    parts: list[str] = []
    for key in sorted(meta.keys()):
        raw = meta[key]
        if raw is None:
            continue
        value = _format_meta_value(raw)
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _format_meta_value(value: Any) -> str:
    if isinstance(value, list):
        # ["a", "b"] → [a,b] — matches the sreda.llm style already used
        # in the codebase; more compact than JSON.
        return "[" + ",".join(str(v) for v in value) + "]"
    if isinstance(value, str):
        if " " in value or "=" in value:
            return f'"{value}"'
        return value
    return str(value)
