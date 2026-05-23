"""Phase B (Issue #68): tests for envelope builder helpers.

Plan: plans/mellow-discovering-conway-final.md — Section 7 (caller pattern).

Tests:
- build_request_envelope — ts, phase=request, attempt, request{messages, tool_schemas,
  provider, model, client_kwargs, bound_layers, bound_kwargs, invocation_kwargs}
- build_response_envelope — phase=response, response{content, tool_calls, ...}, usage
- build_error_envelope — phase=error, error{type, sanitized_msg, latency_ms}
- serialize_response — все fields preserved через _jsonify
- extract_usage — cache_read из nested input_token_details surfaced
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from sreda.services.llm_trace import (
    build_error_envelope, build_request_envelope, build_response_envelope,
    extract_usage, serialize_response,
)


def _base_fields() -> dict:
    return {
        "schema_version": 1,
        "trace_id": "trace_test",
        "run_id": "run_test",
        "iter": 0,
        "tenant_id": "t1",
        "user_id": "u1",
        "feature_key": "housewife_assistant",
    }


class _FakeLLM:
    """Minimal stand-in for ChatOpenAI exposing introspect attrs."""
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


def test_build_request_envelope_has_required_fields():
    llm = _FakeLLM(
        temperature=0.3, top_p=None, seed=None, stop=None, max_tokens=None,
        tool_choice=None, parallel_tool_calls=None, response_format=None,
        extra_body=None, openai_api_base="https://api.x.com/v1",
        model_name="mimo-v2-flash", request_timeout=60.0,
    )
    messages = [
        SystemMessage(content="prompt"),
        HumanMessage(content="привет"),
    ]
    env = build_request_envelope(
        base_fields=_base_fields(),
        attempt="primary",
        messages=messages,
        tool_schemas=[{"type": "function", "function": {"name": "fake"}}],
        llm=llm,
        provider="mimo-flash",
        invocation_kwargs={"timeout_seconds": 60.0},
    )
    assert env["phase"] == "request"
    assert env["attempt"] == "primary"
    assert env["trace_id"] == "trace_test"
    assert env["ts"].endswith("Z")
    req = env["request"]
    assert len(req["messages"]) == 2
    assert req["messages"][0]["type"] == "SystemMessage"
    assert req["tool_schemas"][0]["function"]["name"] == "fake"
    assert req["provider"] == "mimo-flash"
    assert req["model"] == "mimo-v2-flash"
    assert req["client_kwargs"]["temperature"] == 0.3
    assert req["client_kwargs"]["base_url"] == "https://api.x.com/v1"
    assert req["bound_layers"] == []   # no RunnableBinding wrapping
    assert req["bound_kwargs"] == {}
    assert req["invocation_kwargs"] == {"timeout_seconds": 60.0}


def test_build_response_envelope():
    ai_msg = AIMessage(
        content="готово",
        tool_calls=[{"id": "1", "name": "save_recipe",
                     "args": {"title": "X"}, "type": "tool_call"}],
        additional_kwargs={"reasoning_content": "thinking"},
        usage_metadata={
            "input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
            "input_token_details": {"cache_read": 800},
        },
    )
    env = build_response_envelope(
        base_fields=_base_fields(),
        attempt="primary",
        ai_msg=ai_msg,
        latency_ms=2500,
    )
    assert env["phase"] == "response"
    assert env["attempt"] == "primary"
    resp = env["response"]
    assert resp["content"] == "готово"
    assert resp["tool_calls"][0]["name"] == "save_recipe"
    assert resp["additional_kwargs"]["reasoning_content"] == "thinking"
    assert resp["latency_ms"] == 2500
    usage = env["usage"]
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 50
    assert usage["cache_read"] == 800


def test_build_error_envelope():
    exc = ValueError("API rate limit exceeded")
    env = build_error_envelope(
        base_fields=_base_fields(),
        attempt="fallback",
        exc=exc,
        latency_ms=1234,
    )
    assert env["phase"] == "error"
    assert env["attempt"] == "fallback"
    err = env["error"]
    assert err["type"] == "ValueError"
    assert "rate limit" in err["sanitized_msg"]
    assert err["latency_ms"] == 1234


def test_serialize_response_jsonable():
    """All fields из serialize_response должны json.dumps safely."""
    import json
    ai_msg = AIMessage(content="text", tool_calls=[])
    result = serialize_response(ai_msg, latency_ms=100)
    json.dumps(result, ensure_ascii=False)  # MUST NOT raise


def test_extract_usage_zero_when_missing():
    ai_msg = AIMessage(content="x")
    usage = extract_usage(ai_msg)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert "cache_read" not in usage


def test_extract_usage_cache_read_from_nested():
    ai_msg = AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
            "input_token_details": {"cache_read": 75},
        },
    )
    assert extract_usage(ai_msg)["cache_read"] == 75


def test_extract_usage_cached_alias():
    """Legacy alias 'cached' (вместо cache_read)."""
    ai_msg = AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
            "input_token_details": {"cached": 75},
        },
    )
    assert extract_usage(ai_msg)["cache_read"] == 75
