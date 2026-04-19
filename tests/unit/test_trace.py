"""Unit tests for services.trace — per-turn timing log."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import pytest

from sreda.services import trace


@pytest.fixture(autouse=True)
def _reset_trace():
    """Every test starts with no active trace. ContextVar leaks between
    tests otherwise (each test runs in the same asyncio/sync context)."""
    trace.set_current(None)
    yield
    trace.set_current(None)


@pytest.fixture(autouse=True)
def _restore_sreda_trace_propagation():
    """``configure_logging`` sets ``sreda.trace.propagate=False`` when it
    runs in another test earlier. caplog relies on propagation to the
    root logger — force it on for our tests and restore after."""
    lg = logging.getLogger("sreda.trace")
    prev = lg.propagate
    prev_level = lg.level
    lg.propagate = True
    lg.setLevel(logging.INFO)
    yield
    lg.propagate = prev
    lg.setLevel(prev_level)


# ---------------------------------------------------------------------------
# start_trace / current
# ---------------------------------------------------------------------------


def test_start_trace_sets_current():
    ctx = trace.start_trace(user_id="u1", tenant_id="t1")
    assert trace.current() is ctx
    assert ctx.user_id == "u1"
    assert ctx.tenant_id == "t1"
    assert ctx.channel == "telegram"
    assert ctx.trace_id.startswith("trace_")


def test_start_trace_uses_explicit_id():
    ctx = trace.start_trace(trace_id="run_abc123")
    assert ctx.trace_id == "run_abc123"


def test_current_returns_none_before_start():
    assert trace.current() is None


# ---------------------------------------------------------------------------
# step() — timing + meta
# ---------------------------------------------------------------------------


def test_step_records_duration_and_meta():
    trace.start_trace(user_id="u1", tenant_id="t1")
    with trace.step("voice.transcribe", provider="yandex") as m:
        time.sleep(0.05)
        m["bytes_in"] = 1024
        m["chars_out"] = 8

    ctx = trace.current()
    assert len(ctx.events) == 1
    event = ctx.events[0]
    assert event.step == "voice.transcribe"
    assert event.duration_ms >= 45  # allow scheduler jitter
    assert event.meta == {"provider": "yandex", "bytes_in": 1024, "chars_out": 8}


def test_step_without_active_trace_is_noop():
    # No start_trace — should silently run, not crash.
    with trace.step("foo") as m:
        m["x"] = 1
    # Still no trace.
    assert trace.current() is None


def test_record_zero_duration_event():
    trace.start_trace(user_id="u1")
    trace.record("webhook.received", type="text")
    ctx = trace.current()
    assert len(ctx.events) == 1
    assert ctx.events[0].step == "webhook.received"
    assert ctx.events[0].duration_ms == 0
    assert ctx.events[0].meta == {"type": "text"}


def test_record_without_active_trace_is_noop():
    trace.record("foo", bar=1)  # must not crash
    assert trace.current() is None


def test_events_are_time_ordered():
    trace.start_trace()
    trace.record("step1")
    time.sleep(0.01)
    with trace.step("step2"):
        time.sleep(0.02)
    time.sleep(0.01)
    trace.record("step3")

    ctx = trace.current()
    at_ms = [e.at_ms for e in ctx.events]
    assert at_ms == sorted(at_ms)


# ---------------------------------------------------------------------------
# serialize / deserialize round trip (the cross-process handoff)
# ---------------------------------------------------------------------------


def test_serialize_deserialize_round_trip_preserves_events():
    ctx = trace.start_trace(
        trace_id="trace_abc", user_id="u1", tenant_id="t1"
    )
    trace.record("webhook.received", type="voice")
    with trace.step("llm.iter.0", model="mimo-v2") as m:
        time.sleep(0.01)
        m["in_tok"] = 3572
        m["out_tok"] = 91
        m["tools"] = ["save_episode"]

    payload = trace.serialize_for_outbox(ctx)
    # Must be JSON-safe — the worker stores this in payload_json.
    dumped = json.dumps(payload, ensure_ascii=False)
    reloaded = json.loads(dumped)

    restored = trace.deserialize_from_outbox(reloaded)
    assert restored.trace_id == "trace_abc"
    assert restored.user_id == "u1"
    assert restored.tenant_id == "t1"
    assert restored.channel == "telegram"
    assert len(restored.events) == 2
    assert restored.events[0].step == "webhook.received"
    assert restored.events[0].meta == {"type": "voice"}
    assert restored.events[1].step == "llm.iter.0"
    assert restored.events[1].meta["in_tok"] == 3572
    assert restored.events[1].meta["out_tok"] == 91
    assert restored.events[1].meta["tools"] == ["save_episode"]


def test_deserialize_with_missing_fields_gets_defaults():
    restored = trace.deserialize_from_outbox({})
    assert restored.trace_id.startswith("trace_")
    assert restored.channel == "telegram"
    assert restored.user_id is None
    assert restored.events == []


# ---------------------------------------------------------------------------
# emit_block — format + idempotency
# ---------------------------------------------------------------------------


def test_emit_block_renders_expected_sections(caplog):
    ctx = trace.start_trace(
        trace_id="trace_fmt", user_id="u_fmt", tenant_id="t1"
    )
    trace.record("webhook.received", type="text")
    with trace.step("llm.iter.0") as m:
        m["in_tok"] = 100
        m["out_tok"] = 50
        m["tools"] = ["save_episode"]
    with trace.step("llm.iter.1") as m:
        m["in_tok"] = 110
        m["out_tok"] = 20
        m["tools"] = []

    with caplog.at_level(logging.INFO, logger="sreda.trace"):
        trace.emit_block(
            ctx,
            final_event_name="outbox.delivered",
            final_meta={"chat": "123", "status": "ok"},
        )

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    # Header
    assert "=== TRACE trace_fmt" in msg
    assert "user=u_fmt" in msg
    assert "chan=telegram" in msg
    # Each event shows up
    assert "webhook.received" in msg
    assert "llm.iter.0" in msg
    assert "llm.iter.1" in msg
    assert "outbox.delivered" in msg
    # Aggregates
    assert "TOTAL" in msg
    assert "iters=2" in msg
    assert "tok_in=210" in msg
    assert "tok_out=70" in msg


def test_emit_block_is_idempotent(caplog):
    ctx = trace.start_trace(trace_id="trace_idem")
    trace.record("webhook.received")

    with caplog.at_level(logging.INFO, logger="sreda.trace"):
        trace.emit_block(ctx)
        trace.emit_block(ctx)  # second call must be no-op

    assert len(caplog.records) == 1


def test_emit_block_adds_final_event_before_total(caplog):
    ctx = trace.start_trace(trace_id="trace_final")
    trace.record("webhook.received")

    with caplog.at_level(logging.INFO, logger="sreda.trace"):
        trace.emit_block(
            ctx, final_event_name="outbox.delivered",
            final_meta={"status": "ok"},
        )

    # The final event got appended — present in event list now.
    assert any(e.step == "outbox.delivered" for e in ctx.events)
    msg = caplog.records[0].getMessage()
    # ...and shows up ABOVE the TOTAL line.
    body = msg.split("\n")
    outbox_idx = next(i for i, ln in enumerate(body) if "outbox.delivered" in ln)
    total_idx = next(i for i, ln in enumerate(body) if "TOTAL" in ln)
    assert outbox_idx < total_idx


def test_emit_block_with_no_events_still_produces_header_and_total(caplog):
    ctx = trace.start_trace(trace_id="trace_empty")

    with caplog.at_level(logging.INFO, logger="sreda.trace"):
        trace.emit_block(ctx)

    msg = caplog.records[0].getMessage()
    assert "=== TRACE trace_empty" in msg
    assert "TOTAL 0ms" in msg
    assert "iters=0" in msg


def test_meta_with_list_values_renders_compact():
    trace.start_trace()
    trace.record("llm.iter.0", tools=["web_search", "fetch_url"])
    ctx = trace.current()

    with pytest.MonkeyPatch.context() as mp:
        records = []
        mp.setattr(trace.logger, "info", lambda msg: records.append(msg))
        trace.emit_block(ctx)

    assert records
    assert "tools=[web_search,fetch_url]" in records[0]


def test_string_meta_with_spaces_is_quoted():
    trace.start_trace()
    trace.record("voice.transcribe", text="hello world")
    ctx = trace.current()

    with pytest.MonkeyPatch.context() as mp:
        records = []
        mp.setattr(trace.logger, "info", lambda msg: records.append(msg))
        trace.emit_block(ctx)

    assert records
    assert 'text="hello world"' in records[0]


# ---------------------------------------------------------------------------
# set_current / reset
# ---------------------------------------------------------------------------


def test_set_current_replaces_active_trace():
    ctx1 = trace.start_trace(trace_id="a")
    ctx2 = trace.TraceContext(
        trace_id="b", user_id=None, tenant_id=None,
        channel="telegram", started_at=datetime.now(UTC),
        started_monotonic=time.monotonic(),
    )
    trace.set_current(ctx2)
    assert trace.current() is ctx2
    # ctx1 is not emitted via set_current; it still exists as an object
    assert ctx1.trace_id == "a"
