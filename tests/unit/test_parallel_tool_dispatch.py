"""Tests for `runtime.handlers._dispatch_tool_calls_batch` (Phase B).

Verifies:
- Allowlisted batches dispatch in parallel (sub-linear wall-time).
- Mixed / single-tool batches dispatch serial.
- Result order matches tool_calls order regardless of parallel exec.
- Tool exception in one call doesn't fail siblings.
- Unknown tool names return ``error:unknown_tool:<name>`` placeholder.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from sreda.runtime.handlers import (
    _MAX_PARALLEL_DISPATCH,
    _PARALLEL_SAFE_TOOLS,
    _dispatch_one_tool,
    _dispatch_tool_calls_batch,
    _should_dispatch_in_parallel,
)


# ---------------------------------------------------------------------------
# Fake tool helper — emulates LangChain `tool.invoke(args)` interface.
# ---------------------------------------------------------------------------


class _FakeTool:
    """Mimics ``langchain.BaseTool`` — exposes ``.invoke(args) -> str``."""

    def __init__(
        self,
        *,
        sleep_s: float = 0.0,
        result: str = "ok",
        raise_exc: type[BaseException] | None = None,
        record_thread_into: list[str] | None = None,
    ):
        self.sleep_s = sleep_s
        self.result = result
        self.raise_exc = raise_exc
        self.record_thread_into = record_thread_into

    def invoke(self, args: dict[str, Any]) -> str:
        if self.record_thread_into is not None:
            self.record_thread_into.append(threading.current_thread().name)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if self.raise_exc:
            raise self.raise_exc("boom")
        return self.result


# ---------------------------------------------------------------------------
# _should_dispatch_in_parallel — decision gate.
# ---------------------------------------------------------------------------


def test_single_tool_call_never_parallel():
    """Batch of 1 → no thread overhead (overhead > savings)."""
    tcs = [{"name": "fetch_url", "id": "x", "args": {}}]
    assert _should_dispatch_in_parallel(tcs) is False


def test_two_allowlisted_calls_parallel():
    tcs = [
        {"name": "fetch_url", "id": "1", "args": {}},
        {"name": "get_weather", "id": "2", "args": {}},
    ]
    assert _should_dispatch_in_parallel(tcs) is True


def test_mixed_allowlist_and_db_tool_serial():
    """Even one DB-touching tool → fall back to serial (session not threadsafe)."""
    tcs = [
        {"name": "fetch_url", "id": "1", "args": {}},
        {"name": "schedule_reminder", "id": "2", "args": {}},  # NOT allowlisted
    ]
    assert _should_dispatch_in_parallel(tcs) is False


def test_all_db_tools_serial():
    tcs = [
        {"name": "save_core_fact", "id": "1", "args": {}},
        {"name": "schedule_reminder", "id": "2", "args": {}},
    ]
    assert _should_dispatch_in_parallel(tcs) is False


# ---------------------------------------------------------------------------
# _dispatch_one_tool — pure helper.
# ---------------------------------------------------------------------------


def test_dispatch_one_returns_result_string():
    tools = {"fetch_url": _FakeTool(result="hello")}
    tc = {"name": "fetch_url", "id": "x1", "args": {"url": "https://e.com"}}
    tc_id, name, result = _dispatch_one_tool(tc, tools)
    assert tc_id == "x1"
    assert name == "fetch_url"
    assert result == "hello"


def test_dispatch_one_unknown_tool_returns_error_placeholder():
    tc = {"name": "no_such_tool", "id": "x2", "args": {}}
    tc_id, name, result = _dispatch_one_tool(tc, {})
    assert tc_id == "x2"
    assert name == "no_such_tool"
    assert result == "error:unknown_tool:no_such_tool"


def test_dispatch_one_tool_exception_returns_error_placeholder():
    tools = {"fetch_url": _FakeTool(raise_exc=RuntimeError)}
    tc = {"name": "fetch_url", "id": "x3", "args": {}}
    tc_id, name, result = _dispatch_one_tool(tc, tools)
    assert tc_id == "x3"
    assert name == "fetch_url"
    assert result == "error:RuntimeError"


# ---------------------------------------------------------------------------
# _dispatch_tool_calls_batch — integration.
# ---------------------------------------------------------------------------


def test_batch_preserves_order_when_parallel():
    """3 fetch_url calls → ordered (1, 2, 3) matching tool_calls order."""
    tools = {"fetch_url": _FakeTool(result="r")}
    tcs = [
        {"name": "fetch_url", "id": f"id_{i}", "args": {}}
        for i in range(3)
    ]
    results = _dispatch_tool_calls_batch(tcs, tools)
    assert [r[0] for r in results] == ["id_0", "id_1", "id_2"]


class _OrderedFakeTool:
    """Tool returning per-call sleep based on args["delay"] — для проверки
    что order сохраняется когда tool'ы завершаются в обратном порядке.
    """

    def invoke(self, args: dict[str, Any]) -> str:
        delay = float(args.get("delay", 0))
        time.sleep(delay)
        return f"done_after_{delay}"


def test_batch_order_preserved_under_inverse_completion():
    """Codex r1 MINOR + Xiaomi MINOR: forced out-of-order completion.

    First tc sleeps longest, last tc sleeps least. Without order
    preservation, results would arrive in reverse. Verify final list
    still matches tool_calls order — proves submit-order iteration of
    futures (not as_completed).
    """
    tools = {"fetch_url": _OrderedFakeTool()}
    # tc[0] sleeps longest (0.3s), tc[2] sleeps least (0.05s).
    tcs = [
        {"name": "fetch_url", "id": "first_slow",  "args": {"delay": 0.30}},
        {"name": "fetch_url", "id": "middle",     "args": {"delay": 0.15}},
        {"name": "fetch_url", "id": "last_fast",   "args": {"delay": 0.05}},
    ]
    results = _dispatch_tool_calls_batch(tcs, tools)
    assert [r[0] for r in results] == ["first_slow", "middle", "last_fast"]
    assert [r[2] for r in results] == [
        "done_after_0.3", "done_after_0.15", "done_after_0.05",
    ]


def test_batch_parallel_walltime_sublinear():
    """5× sleep(0.2s) parallel → < 5 × 0.2s. Generous slack для CI."""
    tools = {"fetch_url": _FakeTool(sleep_s=0.2, result="r")}
    tcs = [
        {"name": "fetch_url", "id": f"id_{i}", "args": {}}
        for i in range(5)
    ]
    t0 = time.monotonic()
    _dispatch_tool_calls_batch(tcs, tools)
    elapsed = time.monotonic() - t0
    # Serial would be ~1.0s. Parallel should be ~0.2s + thread overhead.
    # CI noise → allow up to 0.7s; serial 1.0s would still fail comfortably.
    assert elapsed < 0.7, f"expected sub-linear, got {elapsed:.2f}s"


def test_batch_serial_walltime_when_mixed():
    """Mixed batch falls back to serial — wall-time ≈ N × per-tool sleep."""
    tools = {
        "fetch_url": _FakeTool(sleep_s=0.1, result="r"),
        "schedule_reminder": _FakeTool(sleep_s=0.1, result="r"),
    }
    tcs = [
        {"name": "fetch_url", "id": "1", "args": {}},
        {"name": "schedule_reminder", "id": "2", "args": {}},
    ]
    t0 = time.monotonic()
    _dispatch_tool_calls_batch(tcs, tools)
    elapsed = time.monotonic() - t0
    # Serial: ~0.2s. Parallel would be ~0.1s. Test that we DON'T parallelize.
    assert elapsed >= 0.18, f"expected serial >= 0.18s, got {elapsed:.2f}s"


def test_batch_parallel_uses_separate_threads():
    """Sanity: parallel dispatch actually runs on different threads."""
    threads_seen: list[str] = []
    tools = {
        "fetch_url": _FakeTool(
            sleep_s=0.1, result="r", record_thread_into=threads_seen,
        ),
    }
    tcs = [
        {"name": "fetch_url", "id": f"id_{i}", "args": {}}
        for i in range(3)
    ]
    _dispatch_tool_calls_batch(tcs, tools)
    # At least 2 distinct threads — main thread не используется,
    # все из ThreadPoolExecutor pool.
    assert len(set(threads_seen)) >= 2


def test_batch_one_tool_exception_doesnt_fail_siblings():
    """Phase B contract: each tool result independent."""
    tools = {
        "fetch_url": _FakeTool(result="ok_a"),
        "get_weather": _FakeTool(raise_exc=ValueError),
    }
    tcs = [
        {"name": "fetch_url", "id": "1", "args": {}},
        {"name": "get_weather", "id": "2", "args": {}},
    ]
    results = _dispatch_tool_calls_batch(tcs, tools)
    assert results[0] == ("1", "fetch_url", "ok_a")
    assert results[1] == ("2", "get_weather", "error:ValueError")


def test_batch_max_workers_capped():
    """Hard cap: даже с batch=20, не больше _MAX_PARALLEL_DISPATCH workers."""
    threads_seen: list[str] = []
    tools = {
        "fetch_url": _FakeTool(
            sleep_s=0.05, result="r", record_thread_into=threads_seen,
        ),
    }
    tcs = [
        {"name": "fetch_url", "id": f"id_{i}", "args": {}}
        for i in range(20)
    ]
    _dispatch_tool_calls_batch(tcs, tools)
    # 20 calls, max_workers=8 → at least some thread reuse, не 20 distinct.
    distinct = len(set(threads_seen))
    assert distinct <= _MAX_PARALLEL_DISPATCH, (
        f"distinct threads {distinct} > cap {_MAX_PARALLEL_DISPATCH}"
    )


# ---------------------------------------------------------------------------
# Allowlist contents — anchor against accidental expansion.
# ---------------------------------------------------------------------------


def test_allowlist_contents_explicit():
    """Pin current allowlist. Extending requires audit — see comments
    above _PARALLEL_SAFE_TOOLS for inclusion criteria."""
    assert _PARALLEL_SAFE_TOOLS == frozenset({"fetch_url", "get_weather"})


def test_max_parallel_dispatch_reasonable():
    """Cap > 1 (otherwise no parallelism) but < 32 (DoS protection)."""
    assert 2 <= _MAX_PARALLEL_DISPATCH <= 32
