"""Phase B (Issue #68): failed LLM call writes phase="error" envelope row.

Plan: plans/mellow-discovering-conway-final.md — Section 3 (caller pattern).

When ainvoke raises (RateLimitError / timeout / OOM):
- handlers.py catches exception
- builds error envelope с {type, sanitized_msg, latency_ms}
- fire-and-forget persist_response_envelope → row на диске
- existing alert + fallback flow continues

Test light: build error envelope manually (как handlers.py делал бы) + persist
+ verify file content. Не запускает full turn pipeline (heavy).
"""
from __future__ import annotations

import json
import pytest

from sreda.services.llm_trace import (
    build_error_envelope, persist_response_envelope, startup_writer, shutdown_drain,
)
from sreda.services import llm_trace as _llm_trace_module


@pytest.fixture(autouse=True)
def _reset_state():
    _llm_trace_module._SHUTDOWN_FLAG = False
    _llm_trace_module._WRITER_TASK = None
    _llm_trace_module._WRITE_QUEUE = None
    _llm_trace_module._GC_TASK = None
    _llm_trace_module._MAIN_LOOP = None
    _llm_trace_module._TRACE_DATES.clear()
    _llm_trace_module._TRACE_SEQ.clear()
    _llm_trace_module._TRACE_LAST_USED.clear()
    _llm_trace_module._WRITER_READY.clear()
    yield
    _llm_trace_module._SHUTDOWN_FLAG = False


@pytest.fixture
def _enable_logging(monkeypatch):
    monkeypatch.setenv("SREDA_LLM_TRACE_LOGGING_ENABLED", "true")
    monkeypatch.delenv("SREDA_LLM_TRACE_REQUIRE_PERSIST", raising=False)
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _base_fields() -> dict:
    return {
        "schema_version": 1,
        "trace_id": "trace_error_test",
        "run_id": "run_e1",
        "iter": 0,
        "tenant_id": "t",
        "user_id": "u",
        "feature_key": "f",
    }


@pytest.mark.asyncio
async def test_rate_limit_error_writes_phase_error_envelope(
    _enable_logging, tmp_path, monkeypatch,
):
    monkeypatch.setattr(_llm_trace_module, "_TRACE_ROOT", tmp_path)
    await startup_writer()
    try:
        # Simulate handlers.py catching primary invoke exception
        try:
            raise RuntimeError("upstream RateLimitError: 429 too many requests")
        except RuntimeError as exc:
            env = build_error_envelope(
                base_fields=_base_fields(),
                attempt="primary",
                exc=exc,
                latency_ms=1234,
            )
            await persist_response_envelope(env)
    finally:
        await shutdown_drain(timeout_seconds=2.0)

    # Verify file written с phase=error
    files = list(tmp_path.rglob("trace_error_test.jsonl"))
    assert len(files) == 1
    line = files[0].read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["phase"] == "error"
    assert row["attempt"] == "primary"
    assert row["error"]["type"] == "RuntimeError"
    assert "RateLimitError" in row["error"]["sanitized_msg"]
    assert row["error"]["latency_ms"] == 1234
    assert row["seq"] == 0


@pytest.mark.asyncio
async def test_primary_error_then_fallback_writes_4_rows(
    _enable_logging, tmp_path, monkeypatch,
):
    """Primary fails, fallback succeeds → 4 envelope rows in file:
    request:primary, error:primary, request:fallback, response:fallback.
    """
    from sreda.services.llm_trace import (
        build_request_envelope, build_response_envelope, persist_request_envelope,
        PersistResult,
    )
    from langchain_core.messages import AIMessage, HumanMessage

    monkeypatch.setattr(_llm_trace_module, "_TRACE_ROOT", tmp_path)
    await startup_writer()

    class _FakeLLM:
        def __init__(self, model: str):
            self.model_name = model
            self.temperature = 0.3
            self.top_p = None; self.seed = None; self.stop = None
            self.max_tokens = None; self.tool_choice = None
            self.parallel_tool_calls = None; self.response_format = None
            self.extra_body = None; self.openai_api_base = "https://x.com"
            self.request_timeout = 60.0

    primary_llm = _FakeLLM("mimo-v2-flash")
    fallback_llm = _FakeLLM("mimo-v2.5-pro")
    messages = [HumanMessage(content="hi")]
    base = {
        "schema_version": 1, "trace_id": "trace_fallback_test",
        "run_id": "r", "iter": 0, "tenant_id": "t", "user_id": "u",
        "feature_key": "f",
    }
    try:
        # 1. primary request
        env1 = build_request_envelope(
            base_fields=base, attempt="primary", messages=messages,
            tool_schemas=[], llm=primary_llm, provider="mimo-flash",
        )
        assert await persist_request_envelope(env1) == PersistResult.WRITTEN

        # 2. primary error
        env2 = build_error_envelope(
            base_fields=base, attempt="primary",
            exc=RuntimeError("primary timeout"), latency_ms=2000,
        )
        await persist_response_envelope(env2)

        # 3. fallback request
        env3 = build_request_envelope(
            base_fields=base, attempt="fallback", messages=messages,
            tool_schemas=[], llm=fallback_llm, provider="mimo-v2.5",
        )
        assert await persist_request_envelope(env3) == PersistResult.WRITTEN

        # 4. fallback response
        fallback_ai = AIMessage(content="resp from fallback", tool_calls=[])
        env4 = build_response_envelope(
            base_fields=base, attempt="fallback", ai_msg=fallback_ai,
            latency_ms=3000,
        )
        await persist_response_envelope(env4)
    finally:
        await shutdown_drain(timeout_seconds=2.0)

    files = list(tmp_path.rglob("trace_fallback_test.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    rows = [json.loads(ln) for ln in lines]
    assert len(rows) == 4
    # Order should be strictly: request:primary, error:primary, request:fallback, response:fallback
    phases_attempts = [(r["phase"], r["attempt"]) for r in rows]
    assert phases_attempts == [
        ("request", "primary"),
        ("error", "primary"),
        ("request", "fallback"),
        ("response", "fallback"),
    ]
    # seq monotonic gap-free
    assert [r["seq"] for r in rows] == [0, 1, 2, 3]
