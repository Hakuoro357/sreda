"""Issue #68 — Full LLM request/response logging + replay support.

Plan: ``plans/mellow-discovering-conway-final.md`` (R7 NO SIGNIFICANT CONCERNS
after 7 Codex rounds + 1 self-review). Decision logs:
``plans/archive/mellow-discovering-conway/decision-log-r{1..6}.md``.

## Архитектура

Per-process **single writer task** + asyncio.Queue + dedicated
ThreadPoolExecutor(max_workers=1). Single-thread inherent FIFO ordering
across все writes (within process). На диск идут JSONL files
``/var/lib/sreda/private/llm-traces/YYYY-MM-DD/{trace_id}.jsonl``.

## API surface

Three submit paths:

- ``persist_request_envelope(envelope) -> PersistResult`` — STRONG semantics.
  Caller **awaits** until disk write completes (or returns degraded result).
  Crash-safety: request row на диске **до** того как мы вызовем LLM.

- ``persist_response_envelope(envelope) -> None`` — WEAK fire-and-forget.
  Returns immediately после queue submit. Used для response/error rows
  где user-facing reply уже delivered.

- ``persist_envelope_sync(envelope) -> None`` — sync path для
  forced-summary turn (handlers.py:2950) и других non-async callers.
  Submits через ``asyncio.run_coroutine_threadsafe``. Deadlock-guarded:
  если caller running ON _MAIN_LOOP → fail-fast log.error (не block).

## Compliance / Privacy

PII (decrypted memories, user text, tool args) на диске в plaintext.
Защита:

- File mode 0600, dir mode 0700, owner ``sreda:sreda``
- Feature flag ``SREDA_LLM_TRACE_LOGGING_ENABLED`` default off
- Compliance-strict ``SREDA_LLM_TRACE_REQUIRE_PERSIST=true`` — refuse LLM
  call если trace not WRITTEN. Settings model_validator catches misconfig
  pair at startup; defensive runtime check returns FAILED if mutated.
- Dedicated private path (FHS convention) — не попадает в default backups
- Cleanup timer удаляет folder'ы старше 5 дней (UTC date parsing,
  не mtime)
- ``scripts/validate_no_backup_leak.py`` deploy gate — Python validator
  с ``Path.resolve()`` canonical paths + ``/`` ancestor handling

## TTL / state

- ``_TRACE_DATES`` — fixed at first row, midnight-safe (request на 23:59
  и response на 00:01 в одной папке)
- ``_TRACE_SEQ`` — monotonic per trace, gap-free (writer thread serializes)
- ``_TRACE_LAST_USED`` — touched на каждый write; GC coroutine 60s drops
  entries > 24h (longer than max realistic trace lifetime)
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)

from sreda.config.settings import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_TRACE_ROOT: Path = Path("/var/lib/sreda/private/llm-traces")
_TRACE_STATE_TTL_SECONDS: float = 24 * 3600
_QUEUE_MAX_SIZE: int = 1000
_REQUEST_PERSIST_TIMEOUT_SECONDS: float = 2.0
_SYNC_OUTER_TIMEOUT_SECONDS: float = 3.0
_GC_INTERVAL_SECONDS: float = 60.0

# Runtime state — initialized в startup_writer()
_WRITE_QUEUE: asyncio.Queue | None = None
_WRITER_TASK: asyncio.Task | None = None
_GC_TASK: asyncio.Task | None = None
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None
_WRITER_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-trace-writer")
_WRITER_READY: asyncio.Event = asyncio.Event()
_SHUTDOWN_FLAG: bool = False

# Per-trace state (thread-safe — touched от writer thread + async tasks)
_TRACE_DATES: dict[str, str] = {}
_TRACE_SEQ: dict[str, int] = {}
_TRACE_LAST_USED: dict[str, float] = {}
_TRACE_STATE_GUARD = threading.Lock()


# ---------------------------------------------------------------------------
# Result enum
# ---------------------------------------------------------------------------


class PersistResult(str, Enum):
    """Result of persist_request_envelope.

    Caller policy:
    - WRITTEN — proceed с LLM call
    - DROPPED / TIMEOUT / FAILED — send admin alert. Если
      ``SREDA_LLM_TRACE_REQUIRE_PERSIST=true`` — refuse LLM call.
    """
    WRITTEN = "written"
    DROPPED = "dropped"      # queue full / writer not ready / shutdown
    TIMEOUT = "timeout"      # writer slow > 2s
    FAILED = "failed"        # internal exception during write


# ---------------------------------------------------------------------------
# JSON-safe normalization
# ---------------------------------------------------------------------------


def _jsonify(v: Any) -> Any:
    """Recursive JSON-safe transform. Apply ДО json.dumps для known-bad types.

    Handles bytes (base64), pydantic models (.model_dump), dataclasses,
    datetime/date (.isoformat), list/tuple/set (recurse), dict (recurse +
    stringify keys), unknown objects (repr truncated to 500 chars).

    Used both directly and as ``json.dumps(..., default=_jsonify)``.
    """
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, bytes):
        return {"__bytes_b64__": base64.b64encode(v).decode("ascii")}
    try:
        from pydantic import BaseModel
        if isinstance(v, BaseModel):
            return _jsonify(v.model_dump())
    except ImportError:
        pass
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _jsonify(dataclasses.asdict(v))
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_jsonify(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonify(val) for k, val in v.items()}
    return {"__unrepr__": type(v).__name__, "__repr__": repr(v)[:500]}


# ---------------------------------------------------------------------------
# Message serialization (round-trip)
# ---------------------------------------------------------------------------


def _msg_to_jsonable(msg: BaseMessage) -> dict:
    """Serialize BaseMessage → JSON-safe dict.

    Round-trip contract: ``_msg_from_jsonable(_msg_to_jsonable(m)) ≈ m``
    structurally. Preserves all base fields (id, name, additional_kwargs,
    response_metadata, content) + type-specific fields.

    Important: ``content`` может быть либо ``str`` либо ``list[dict]``
    (multi-part с ``cache_control`` blocks для prompt caching). Layout
    survives round-trip без потерь.
    """
    base: dict[str, Any] = {
        "type": type(msg).__name__,
        "content": _jsonify(msg.content),
        "additional_kwargs": _jsonify(dict(msg.additional_kwargs or {})),
        "response_metadata": _jsonify(dict(getattr(msg, "response_metadata", None) or {})),
        "id": getattr(msg, "id", None),
        "name": getattr(msg, "name", None),
    }
    if isinstance(msg, AIMessage):
        base["tool_calls"] = _jsonify(list(msg.tool_calls or []))
        base["invalid_tool_calls"] = _jsonify(list(getattr(msg, "invalid_tool_calls", []) or []))
    elif isinstance(msg, ToolMessage):
        base["tool_call_id"] = msg.tool_call_id
        base["status"] = getattr(msg, "status", None)
        base["artifact"] = _jsonify(getattr(msg, "artifact", None))
    return base


def _msg_from_jsonable(d: dict) -> BaseMessage:
    """Inverse of ``_msg_to_jsonable``. Dispatch by ``d['type']``."""
    msg_type = d.get("type")
    kwargs = {
        "content": d["content"],
        "additional_kwargs": d.get("additional_kwargs") or {},
        "response_metadata": d.get("response_metadata") or {},
        "id": d.get("id"),
        "name": d.get("name"),
    }
    # Drop None id/name — LangChain init defaults handle их
    if kwargs.get("id") is None:
        kwargs.pop("id")
    if kwargs.get("name") is None:
        kwargs.pop("name")

    if msg_type == "SystemMessage":
        return SystemMessage(**kwargs)
    if msg_type == "HumanMessage":
        return HumanMessage(**kwargs)
    if msg_type == "AIMessage":
        return AIMessage(
            **kwargs,
            tool_calls=d.get("tool_calls") or [],
            invalid_tool_calls=d.get("invalid_tool_calls") or [],
        )
    if msg_type == "ToolMessage":
        return ToolMessage(
            **kwargs,
            tool_call_id=d["tool_call_id"],
            status=d.get("status"),
            artifact=d.get("artifact"),
        )
    raise ValueError(f"unknown message type: {msg_type!r}")


# ---------------------------------------------------------------------------
# Provider config introspection (recursive RunnableBinding unwrap)
# ---------------------------------------------------------------------------


def _extract_llm_kwargs(llm: Any) -> dict:
    """Capture full LLM client config for replay.

    LangChain may wrap base model в multiple ``RunnableBinding`` layers
    (от ``.bind_tools(...)``, ``.bind(...)``, etc). Each layer кэширует
    ``.kwargs`` которые передаются в каждый invoke. We walk outermost-first,
    collecting kwargs into ordered list, then merge с verified precedence
    (innermost first, outer overrides → outer wins).

    Returns dict с тремя ключами:
    - ``client_kwargs``: base model attrs (temperature, base_url, model, ...)
    - ``bound_layers``: list of dicts, outermost first (preserve order)
    - ``bound_kwargs``: merged dict, outer-wins precedence

    Replay reconstructs final precedence:
        merged = {**client_kwargs, **bound_kwargs, **invocation_kwargs}
    """
    layers: list[dict] = []
    cur = llm
    # Walk nested RunnableBinding
    while hasattr(cur, "bound") and hasattr(cur, "kwargs"):
        try:
            layer_kwargs = dict(getattr(cur, "kwargs", {}) or {})
        except Exception:
            layer_kwargs = {}
        layers.append(_jsonify(layer_kwargs))
        cur = cur.bound
    # cur = underlying model (ChatOpenAI / MimoChatOpenAI / etc)
    client_kwargs = {
        "temperature": getattr(cur, "temperature", None),
        "top_p": getattr(cur, "top_p", None),
        "seed": getattr(cur, "seed", None),
        "stop": getattr(cur, "stop", None),
        "max_tokens": getattr(cur, "max_tokens", None),
        "tool_choice": getattr(cur, "tool_choice", None),
        "parallel_tool_calls": getattr(cur, "parallel_tool_calls", None),
        "response_format": getattr(cur, "response_format", None),
        "extra_body": _jsonify(getattr(cur, "extra_body", None) or getattr(cur, "model_kwargs", {}) or None),
        "base_url": getattr(cur, "openai_api_base", None),
        "model": getattr(cur, "model_name", getattr(cur, "model", None)),
        "timeout_seconds": getattr(cur, "request_timeout", None),
    }
    # Merge с outer-wins precedence: innermost first, outer overrides
    bound_merged: dict[str, Any] = {}
    for layer in reversed(layers):
        bound_merged.update(layer)
    return {
        "client_kwargs": _jsonify(client_kwargs),
        "bound_layers": layers,
        "bound_kwargs": bound_merged,
    }


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _open_trace_file(path: Path) -> int:
    """Open trace file для append с гарантированным mode 0600.

    Atomic create через ``os.open(O_CREAT | 0o600)``. Idempotent fchmod для
    existing files — protects от umask race на pre-existing files (e.g.
    после manual touch или backup restore с wrong perms).

    Returns file descriptor (caller wraps в ``os.fdopen``).
    """
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        st = os.fstat(fd)
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    return fd


def _write_envelope(envelope: dict) -> None:
    """Sync write of one envelope row. Called by writer thread.

    Assigns ``seq`` (monotonic per trace) and ``trace_started_at_utc``
    (fixed at first row). Touches ``_TRACE_LAST_USED`` для TTL GC.
    """
    trace_id = envelope["trace_id"]
    with _TRACE_STATE_GUARD:
        if trace_id not in _TRACE_DATES:
            _TRACE_DATES[trace_id] = datetime.now(timezone.utc).date().isoformat()
        folder_date = _TRACE_DATES[trace_id]
        seq = _TRACE_SEQ.get(trace_id, 0)
        _TRACE_SEQ[trace_id] = seq + 1
        _TRACE_LAST_USED[trace_id] = time.monotonic()
    envelope = {**envelope, "trace_started_at_utc": folder_date, "seq": seq}

    path = _TRACE_ROOT / folder_date / f"{trace_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = json.dumps(envelope, ensure_ascii=False, default=_jsonify)
    fd = _open_trace_file(path)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Writer task lifecycle
# ---------------------------------------------------------------------------


async def startup_writer() -> None:
    """Eager bootstrap. Call from FastAPI lifespan startup.

    Idempotent — повторный call no-op если writer task running.
    """
    global _WRITE_QUEUE, _WRITER_TASK, _GC_TASK, _MAIN_LOOP, _SHUTDOWN_FLAG
    if _WRITER_TASK is not None and not _WRITER_TASK.done():
        _WRITER_READY.set()
        return
    _SHUTDOWN_FLAG = False
    _MAIN_LOOP = asyncio.get_running_loop()
    _WRITE_QUEUE = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
    _WRITER_TASK = asyncio.create_task(_writer_loop(), name="llm-trace-writer")
    _GC_TASK = asyncio.create_task(_trace_state_gc_loop(), name="llm-trace-state-gc")
    _WRITER_READY.set()
    logger.info("llm-trace writer started, queue maxsize=%d", _QUEUE_MAX_SIZE)


async def shutdown_drain(timeout_seconds: float = 10.0) -> None:
    """Drain queue, cancel writer/GC tasks, shutdown executor.

    Call from FastAPI lifespan shutdown handler. Waits ``timeout_seconds``
    for ``queue.join()`` — затем cancel tasks regardless. Logs warn если
    были non-drained envelopes (data loss).
    """
    global _SHUTDOWN_FLAG, _WRITER_TASK, _GC_TASK
    _SHUTDOWN_FLAG = True
    if _WRITE_QUEUE is not None:
        try:
            await asyncio.wait_for(_WRITE_QUEUE.join(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            qsize = _WRITE_QUEUE.qsize()
            logger.warning(
                "llm-trace queue drain timeout, %d envelopes lost", qsize,
            )
    if _WRITER_TASK is not None and not _WRITER_TASK.done():
        _WRITER_TASK.cancel()
        try:
            await _WRITER_TASK
        except (asyncio.CancelledError, Exception):
            pass
    if _GC_TASK is not None and not _GC_TASK.done():
        _GC_TASK.cancel()
        try:
            await _GC_TASK
        except (asyncio.CancelledError, Exception):
            pass
    # Don't shutdown executor — process-shutdown handles it. Calling
    # shutdown_drain multiple times без full process exit (e.g. test
    # teardown) shouldn't kill _WRITER_EXECUTOR — startup_writer re-uses it.
    _WRITER_READY.clear()


async def _writer_loop() -> None:
    """Single consumer for _WRITE_QUEUE. Submits write через executor,
    awaits future, resolves caller's future (или sets exception).
    """
    while True:
        item = await _WRITE_QUEUE.get()
        envelope, caller_future = item
        if caller_future is not None and caller_future.cancelled():
            # persist_request_envelope отдал вызывающему TIMEOUT —
            # asyncio.wait_for отменил его future. По контракту
            # «WRITTEN ⇔ можно звонить» (require_persist) envelope
            # НЕ должен появиться на диске: иначе останется request-
            # строка для LLM-вызова, который никогда не выполнялся
            # (аудит 2026-07-18, llm-core MINOR #10). Race с уже
            # in-flight записью в executor неизбежен и безопасен.
            logger.debug(
                "llm-trace skipping envelope with timed-out caller trace_id=%s",
                envelope.get("trace_id"),
            )
            _WRITE_QUEUE.task_done()
            continue
        try:
            loop = asyncio.get_running_loop()
            write_future = loop.run_in_executor(
                _WRITER_EXECUTOR, _write_envelope, envelope,
            )
            await write_future
            if caller_future is not None and not caller_future.done():
                caller_future.set_result(None)
        except asyncio.CancelledError:
            if caller_future is not None and not caller_future.done():
                caller_future.cancel()
            raise
        except Exception as e:
            logger.exception(
                "llm-trace writer exception trace_id=%s phase=%s",
                envelope.get("trace_id"), envelope.get("phase"),
            )
            if caller_future is not None and not caller_future.done():
                caller_future.set_exception(e)
        finally:
            _WRITE_QUEUE.task_done()


async def _trace_state_gc_loop() -> None:
    """Periodically drop expired trace state. Runs while writer alive."""
    while not _SHUTDOWN_FLAG:
        try:
            await asyncio.sleep(_GC_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        cutoff = time.monotonic() - _TRACE_STATE_TTL_SECONDS
        with _TRACE_STATE_GUARD:
            stale = [tid for tid, ts in _TRACE_LAST_USED.items() if ts < cutoff]
            for tid in stale:
                _TRACE_DATES.pop(tid, None)
                _TRACE_SEQ.pop(tid, None)
                _TRACE_LAST_USED.pop(tid, None)
        if stale:
            logger.debug("llm-trace state GC removed %d expired traces", len(stale))


# ---------------------------------------------------------------------------
# Public submit API
# ---------------------------------------------------------------------------


async def persist_request_envelope(envelope: dict) -> PersistResult:
    """STRONG semantics — caller awaits actual disk write.

    Returns ``PersistResult`` indicating outcome. Never raises.

    - WRITTEN — envelope persisted (or no-op success when feature disabled)
    - DROPPED — queue full / writer not ready / shutdown
    - TIMEOUT — write didn't complete within 2s; ещё не записанный
      envelope отбрасывается writer'ом (см. ``_writer_loop``), чтобы
      TIMEOUT не оставлял request-строку для отменённого LLM-вызова
    - FAILED — internal exception (defensive)

    Caller checks result + sends admin alert если != WRITTEN. Если
    ``SREDA_LLM_TRACE_REQUIRE_PERSIST=true`` — abort LLM call on degraded.
    """
    if _SHUTDOWN_FLAG:
        return PersistResult.DROPPED
    s = get_settings()
    if not s.llm_trace_logging_enabled:
        if s.llm_trace_require_persist:
            # Defense in depth — validator catches at startup, but if config
            # mutated post-construction (rare) we fail-safe here.
            logger.error(
                "llm_trace_require_persist=true but logging_enabled=false; "
                "returning FAILED (validator should have caught at startup)",
            )
            return PersistResult.FAILED
        return PersistResult.WRITTEN   # legitimate no-op
    if not _WRITER_READY.is_set():
        try:
            await asyncio.wait_for(_WRITER_READY.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning("llm-trace writer not ready, dropping request envelope")
            return PersistResult.DROPPED
    try:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        assert _WRITE_QUEUE is not None
        try:
            _WRITE_QUEUE.put_nowait((envelope, future))
        except asyncio.QueueFull:
            return PersistResult.DROPPED
        try:
            await asyncio.wait_for(future, timeout=_REQUEST_PERSIST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return PersistResult.TIMEOUT
        return PersistResult.WRITTEN
    except Exception:
        logger.exception("llm-trace request persist failed unexpectedly")
        return PersistResult.FAILED


async def persist_response_envelope(envelope: dict) -> None:
    """WEAK semantics — fire-and-forget. Submits envelope в queue, returns
    immediately. Used для response/error rows где user-facing reply уже
    delivered to TG/MAX и worst case loss = no debug artifact.
    """
    if _SHUTDOWN_FLAG or not get_settings().llm_trace_logging_enabled:
        return
    if not _WRITER_READY.is_set():
        return
    try:
        future = None  # fire-and-forget — no caller waits
        assert _WRITE_QUEUE is not None
        try:
            _WRITE_QUEUE.put_nowait((envelope, future))
        except asyncio.QueueFull:
            logger.warning(
                "llm-trace queue full, dropping response/error envelope trace_id=%s",
                envelope.get("trace_id"),
            )
            return
    except Exception:
        logger.exception("llm-trace response submit failed")


def extract_usage(ai_msg: Any) -> dict:
    """Pull usage_metadata fields from AIMessage в plain dict.

    LangChain stores cache_read как ``usage_metadata.input_token_details.cache_read``
    (OpenAI-style). MiMo возвращает то же поле — surface'им для replay diff.
    """
    usage = getattr(ai_msg, "usage_metadata", None) or {}
    input_details = usage.get("input_token_details") or {}
    out: dict[str, Any] = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }
    cache_read = input_details.get("cache_read") or input_details.get("cached")
    if cache_read:
        out["cache_read"] = int(cache_read)
    return out


def serialize_response(ai_msg: Any, *, latency_ms: int) -> dict:
    """Convert AIMessage response → JSON-safe dict для phase="response" envelope."""
    return {
        "content": _jsonify(getattr(ai_msg, "content", "") or ""),
        "tool_calls": _jsonify(list(getattr(ai_msg, "tool_calls", None) or [])),
        "invalid_tool_calls": _jsonify(list(getattr(ai_msg, "invalid_tool_calls", []) or [])),
        "additional_kwargs": _jsonify(dict(getattr(ai_msg, "additional_kwargs", {}) or {})),
        "response_metadata": _jsonify(dict(getattr(ai_msg, "response_metadata", {}) or {})),
        "id": getattr(ai_msg, "id", None),
        "name": getattr(ai_msg, "name", None),
        "latency_ms": latency_ms,
    }


def build_request_envelope(
    *,
    base_fields: dict,
    attempt: str,
    messages: list,
    tool_schemas: list[dict],
    llm: Any,
    provider: str | None,
    invocation_kwargs: dict | None = None,
) -> dict:
    """Build phase="request" envelope ready for persist_request_envelope.

    ``base_fields`` carries trace_id/run_id/iter/tenant_id/user_id/feature_key.
    ``messages`` serialized via _msg_to_jsonable. ``llm`` introspected via
    _extract_llm_kwargs (recursive RunnableBinding unwrap).
    """
    llm_kwargs = _extract_llm_kwargs(llm)
    return {
        **base_fields,
        "phase": "request",
        "attempt": attempt,
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
              .replace("+00:00", "Z"),
        "request": {
            "messages": [_msg_to_jsonable(m) for m in messages],
            "tool_schemas": tool_schemas,
            "provider": provider,
            "model": llm_kwargs["client_kwargs"].get("model"),
            "client_kwargs": llm_kwargs["client_kwargs"],
            "bound_layers": llm_kwargs["bound_layers"],
            "bound_kwargs": llm_kwargs["bound_kwargs"],
            "invocation_kwargs": _jsonify(invocation_kwargs or {}),
        },
    }


def build_response_envelope(
    *,
    base_fields: dict,
    attempt: str,
    ai_msg: Any,
    latency_ms: int,
) -> dict:
    """Build phase="response" envelope. Fire-and-forget submit."""
    return {
        **base_fields,
        "phase": "response",
        "attempt": attempt,
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
              .replace("+00:00", "Z"),
        "response": serialize_response(ai_msg, latency_ms=latency_ms),
        "usage": extract_usage(ai_msg),
    }


def build_error_envelope(
    *,
    base_fields: dict,
    attempt: str,
    exc: BaseException,
    latency_ms: int,
) -> dict:
    """Build phase="error" envelope для failed LLM call."""
    return {
        **base_fields,
        "phase": "error",
        "attempt": attempt,
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
              .replace("+00:00", "Z"),
        "error": {
            "type": type(exc).__name__,
            "sanitized_msg": str(exc)[:500],
            "latency_ms": latency_ms,
        },
    }


def persist_envelope_sync(envelope: dict) -> None:
    """Sync path для forced-summary turn (handlers.py:2950) и non-async callers.

    Submits через ``asyncio.run_coroutine_threadsafe`` в _MAIN_LOOP. Blocks
    на future result up to 3s.

    **Deadlock guard**: detects если caller IS на _MAIN_LOOP. Это создало бы
    self-block (waiting on own loop) → fail-fast log.error.
    """
    if _SHUTDOWN_FLAG or not get_settings().llm_trace_logging_enabled:
        return
    if _MAIN_LOOP is None or _WRITE_QUEUE is None:
        logger.warning("llm-trace sync called before writer init — drop")
        return
    # Deadlock guard
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _MAIN_LOOP:
        logger.error(
            "persist_envelope_sync called from main loop — use await persist_envelope. "
            "trace_id=%s phase=%s",
            envelope.get("trace_id"), envelope.get("phase"),
        )
        return

    async def _submit() -> None:
        assert _MAIN_LOOP is not None
        assert _WRITE_QUEUE is not None
        future = _MAIN_LOOP.create_future()
        try:
            _WRITE_QUEUE.put_nowait((envelope, future))
        except asyncio.QueueFull:
            return
        try:
            await asyncio.wait_for(future, timeout=_REQUEST_PERSIST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            pass

    try:
        cf = asyncio.run_coroutine_threadsafe(_submit(), _MAIN_LOOP)
        cf.result(timeout=_SYNC_OUTER_TIMEOUT_SECONDS)
    except Exception:
        logger.exception(
            "llm-trace sync submit failed trace_id=%s",
            envelope.get("trace_id"),
        )
