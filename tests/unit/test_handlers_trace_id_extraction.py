"""Regression test for Codex PR-48 CRITICAL finding (Issue #68).

Bug: ``handlers.py:2578`` used to do ``_trace_id_value = trace.current()
or 'trace_unknown'`` — but ``trace.current()`` returns ``TraceContext |
None``, not a string. ``TraceContext`` is a ``@dataclass(slots=True)``
without ``frozen=True``, so Python sets ``__hash__ = None`` → it is
**unhashable**. Storing it as ``trace_id`` in the envelope means:

  1. ``llm_trace._open_trace_file`` does ``if trace_id not in _TRACE_DATES:``
     → ``TypeError: unhashable type: 'TraceContext'`` → persist fails.
  2. Admin alert body interpolates ``trace_id`` directly → leaks the full
     dataclass repr (events list, monotonic timestamps) into TG message.

This test ensures we always work with the string ``.trace_id`` attribute,
never with the ``TraceContext`` object itself, in any path that
participates in trace logging.
"""
from __future__ import annotations

import pytest

from sreda.services import trace as trace_mod
from sreda.services.llm_trace import _TRACE_DATES, _TRACE_SEQ


@pytest.fixture(autouse=True)
def _reset_trace_state():
    """Isolate from any state leaked by other tests in the module."""
    _TRACE_DATES.clear()
    _TRACE_SEQ.clear()
    yield
    _TRACE_DATES.clear()
    _TRACE_SEQ.clear()


def test_trace_context_is_unhashable():
    """Verify the language-level fact that motivates the fix: dataclass
    instances with ``slots=True`` + default ``eq=True`` + ``frozen=False``
    are unhashable. If this breaks (e.g. CPython changes semantics or
    someone adds ``eq=False`` to ``TraceContext``), this test alerts us
    that the unhashable invariant changed."""
    ctx = trace_mod.start_trace(
        trace_id="trace_unhashable_check",
        user_id="u",
        tenant_id="t",
    )
    with pytest.raises(TypeError, match="unhashable type"):
        _ = {ctx: "value"}


def test_string_trace_id_works_as_dict_key():
    """Positive control: the ``.trace_id`` string attribute works fine
    as a dict key — same operation as ``llm_trace._open_trace_file``."""
    ctx = trace_mod.start_trace(
        trace_id="trace_hashable_check",
        user_id="u",
        tenant_id="t",
    )
    extracted = ctx.trace_id
    assert isinstance(extracted, str)
    assert extracted == "trace_hashable_check"

    # Same pattern as llm_trace.py:333-334
    if extracted not in _TRACE_DATES:
        _TRACE_DATES[extracted] = "2026-05-23"
    assert _TRACE_DATES[extracted] == "2026-05-23"


def test_handlers_extraction_pattern_yields_string():
    """Replicate the exact extraction pattern from ``handlers.py`` after
    the fix. Should always produce a non-empty string suitable for use
    as ``_TRACE_DATES`` dict key."""
    # Path A: trace context present
    trace_mod.start_trace(
        trace_id="trace_via_extraction",
        user_id="u",
        tenant_id="t",
    )
    ctx = trace_mod.current()
    if ctx is not None and getattr(ctx, "trace_id", None):
        result = ctx.trace_id
    else:
        from uuid import uuid4
        result = f"trace_{uuid4().hex[:16]}"
    assert isinstance(result, str)
    assert result == "trace_via_extraction"
    # Confirm it's hashable (would fail with TypeError otherwise)
    _ = {result: 1}


def test_handlers_extraction_pattern_fallback_when_no_context():
    """Path B: orphan invocation (CLI / test harness) without
    ``start_trace``. ``trace.current()`` returns None; fallback synthesizes
    a fresh trace_xxxx id. Must still be a string."""
    # No start_trace — ContextVar default is None.
    # We need a fresh ContextVar — pytest scopes preserve context across
    # the same module, so this could leak. Force-set to None to be safe.
    trace_mod._current_trace.set(None)

    ctx = trace_mod.current()
    if ctx is not None and getattr(ctx, "trace_id", None):
        result = ctx.trace_id
    else:
        from uuid import uuid4
        result = f"trace_{uuid4().hex[:16]}"
    assert isinstance(result, str)
    assert result.startswith("trace_")
    assert len(result) > len("trace_")  # has the uuid suffix
    # Hashable
    _ = {result: 1}
