"""Unit tests for sreda.runtime.llm_caller.LlmCaller.

All tests are pure-unit: no DB, no real LLM calls.
Mocks are applied with monkeypatch.setattr per project rule.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from sreda.runtime.llm_caller import LlmCaller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_caller(fallback_llm=None, feature_key="feat") -> LlmCaller:
    return LlmCaller(
        primary_llm=MagicMock(name="primary_llm"),
        fallback_llm=fallback_llm,
        primary_provider="primary_prov",
        fallback_provider="fallback_prov" if fallback_llm else None,
        per_call_timeout=10.0,
        tenant_id="t1",
        feature_key=feature_key,
    )


def _make_callables():
    persist_request = AsyncMock()
    persist_response = AsyncMock()
    persist_error = AsyncMock()
    return persist_request, persist_response, persist_error


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# (a) Primary success: returns ai_msg, persist_request + persist_response
#     called once each, persist_error not called.
# ---------------------------------------------------------------------------

def test_primary_success(monkeypatch):
    ai_msg = MagicMock(name="ai_msg")
    persist_request, persist_response, persist_error = _make_callables()
    trace_meta: dict = {}

    monkeypatch.setattr(
        "sreda.runtime.llm_caller.ainvoke_with_streaming_timeout",
        AsyncMock(return_value=ai_msg),
    )
    # send_admin_alert is imported locally inside LlmCaller; patch at source.
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert",
        MagicMock(),
        raising=False,
    )

    caller = _make_caller()
    result = _run(caller.ainvoke_with_fallback(
        messages=[],
        iter_n=0,
        trace_meta=trace_meta,
        on_text_update=None,
        persist_request=persist_request,
        persist_response=persist_response,
        persist_error=persist_error,
    ))

    assert result is ai_msg
    persist_request.assert_awaited_once()
    persist_response.assert_awaited_once()
    persist_error.assert_not_awaited()
    # trace_meta untouched on primary success
    assert "fallback" not in trace_meta
    assert "primary_exc" not in trace_meta


# ---------------------------------------------------------------------------
# (b) Primary raises + fallback succeeds → fallback ai_msg,
#     trace_meta["fallback"]=True, primary_exc set, P1 alert.
# ---------------------------------------------------------------------------

def test_primary_fails_fallback_succeeds(monkeypatch):
    primary_exc = RuntimeError("primary boom")
    fallback_ai_msg = MagicMock(name="fallback_ai_msg")
    fallback_llm = MagicMock(name="fallback_llm")
    persist_request, persist_response, persist_error = _make_callables()
    trace_meta: dict = {}
    alert_mock = MagicMock()

    invoke_mock = AsyncMock(side_effect=[primary_exc, fallback_ai_msg])
    monkeypatch.setattr(
        "sreda.runtime.llm_caller.ainvoke_with_streaming_timeout",
        invoke_mock,
    )

    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", alert_mock, raising=False,
    )
    caller = _make_caller(fallback_llm=fallback_llm)
    result = _run(caller.ainvoke_with_fallback(
        messages=[],
        iter_n=2,
        trace_meta=trace_meta,
        on_text_update=None,
        persist_request=persist_request,
        persist_response=persist_response,
        persist_error=persist_error,
    ))

    assert result is fallback_ai_msg
    assert trace_meta.get("fallback") is True
    assert trace_meta.get("primary_exc") == "RuntimeError"

    # persist_request called for primary then fallback
    assert persist_request.await_count == 2
    first_call_attempt = persist_request.await_args_list[0].args[0]
    second_call_attempt = persist_request.await_args_list[1].args[0]
    assert first_call_attempt == "primary"
    assert second_call_attempt == "fallback"

    # persist_error called once (primary), persist_response once (fallback)
    persist_error.assert_awaited_once()
    assert persist_error.await_args.args[0] == "primary"
    persist_response.assert_awaited_once()
    assert persist_response.await_args.args[0] == "fallback"

    # P1 alert fired
    alert_mock.assert_called_once()
    call_kwargs = alert_mock.call_args
    assert call_kwargs.kwargs["severity"] == "P1"
    assert "LLM fallback engaged" in call_kwargs.kwargs["title"]
    assert call_kwargs.kwargs["dedupe_key"] == "llm_fallback:RuntimeError:feat"


# ---------------------------------------------------------------------------
# (c) Primary raises + fallback is None → re-raises, P0 alert.
# ---------------------------------------------------------------------------

def test_primary_fails_no_fallback_reraises(monkeypatch):
    primary_exc = ValueError("no fallback boom")
    persist_request, persist_response, persist_error = _make_callables()
    trace_meta: dict = {}
    alert_mock = MagicMock()

    monkeypatch.setattr(
        "sreda.runtime.llm_caller.ainvoke_with_streaming_timeout",
        AsyncMock(side_effect=primary_exc),
    )

    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", alert_mock, raising=False,
    )
    caller = _make_caller(fallback_llm=None)
    with pytest.raises(ValueError, match="no fallback boom"):
        _run(caller.ainvoke_with_fallback(
            messages=[],
            iter_n=1,
            trace_meta=trace_meta,
            on_text_update=None,
            persist_request=persist_request,
            persist_response=persist_response,
            persist_error=persist_error,
        ))

    # P0 alert fired
    alert_mock.assert_called_once()
    call_kwargs = alert_mock.call_args
    assert call_kwargs.kwargs["severity"] == "P0"
    assert "NO fallback" in call_kwargs.kwargs["title"]
    assert call_kwargs.kwargs["dedupe_key"] == "llm_primary_no_fallback:ValueError:feat"

    # persist_error called (primary), persist_response NOT called
    persist_error.assert_awaited_once()
    persist_response.assert_not_awaited()


# ---------------------------------------------------------------------------
# (d) Primary raises + fallback raises → re-raises (fallback error persisted).
# ---------------------------------------------------------------------------

def test_primary_fails_fallback_fails_reraises(monkeypatch):
    primary_exc = RuntimeError("primary fail")
    fallback_exc = RuntimeError("fallback fail")
    fallback_llm = MagicMock(name="fallback_llm")
    persist_request, persist_response, persist_error = _make_callables()
    trace_meta: dict = {}

    invoke_mock = AsyncMock(side_effect=[primary_exc, fallback_exc])
    monkeypatch.setattr(
        "sreda.runtime.llm_caller.ainvoke_with_streaming_timeout",
        invoke_mock,
    )

    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", MagicMock(), raising=False,
    )
    caller = _make_caller(fallback_llm=fallback_llm)
    with pytest.raises(RuntimeError, match="fallback fail"):
        _run(caller.ainvoke_with_fallback(
            messages=[],
            iter_n=0,
            trace_meta=trace_meta,
            on_text_update=None,
            persist_request=persist_request,
            persist_response=persist_response,
            persist_error=persist_error,
        ))

    # persist_error called twice: primary + fallback
    assert persist_error.await_count == 2
    assert persist_error.await_args_list[0].args[0] == "primary"
    assert persist_error.await_args_list[1].args[0] == "fallback"
    # persist_response never called (both failed)
    persist_response.assert_not_awaited()


# ---------------------------------------------------------------------------
# (e) persist_request called for primary BEFORE invoke.
# ---------------------------------------------------------------------------

def test_persist_request_called_before_invoke(monkeypatch):
    """Ordering: persist_request(primary) must complete before ainvoke."""
    call_order: list[str] = []

    async def fake_persist_request(attempt, *args, **kwargs):
        call_order.append(f"persist_request:{attempt}")

    async def fake_invoke(llm, messages, *, timeout_seconds, on_text_update, provider=None):
        call_order.append("invoke")
        return MagicMock(name="ai_msg")

    async def fake_persist_response(*args, **kwargs):
        call_order.append("persist_response")

    monkeypatch.setattr(
        "sreda.runtime.llm_caller.ainvoke_with_streaming_timeout",
        fake_invoke,
    )

    caller = _make_caller()
    _run(caller.ainvoke_with_fallback(
        messages=[],
        iter_n=0,
        trace_meta={},
        on_text_update=None,
        persist_request=fake_persist_request,
        persist_response=fake_persist_response,
        persist_error=AsyncMock(),
    ))

    assert call_order[0] == "persist_request:primary", (
        f"persist_request:primary must be first, got: {call_order}"
    )
    assert call_order[1] == "invoke", (
        f"invoke must follow persist_request:primary, got: {call_order}"
    )


# ---------------------------------------------------------------------------
# (f) Fallback path: exact arg surface + ordering (Codex A/B #5 R1 MINOR).
#     persist_request("fallback") carries the fallback llm/provider/timeout and
#     runs BEFORE the fallback invoke, which targets the fallback llm.
# ---------------------------------------------------------------------------

def test_fallback_path_args_and_ordering(monkeypatch):
    primary_exc = RuntimeError("primary boom")
    fallback_ai_msg = MagicMock(name="fallback_ai_msg")
    fallback_llm = MagicMock(name="fallback_llm")
    trace_meta: dict = {}
    call_order: list[str] = []

    async def fake_persist_request(attempt, llm_obj, provider, iter_n, timeout):
        call_order.append(f"persist_request:{attempt}")
        if attempt == "fallback":
            # exact arg surface for the fallback envelope
            assert llm_obj is fallback_llm
            assert provider == "fallback_prov"
            assert timeout == 10.0
            assert iter_n == 3

    invoked_llms: list = []

    async def fake_invoke(llm, messages, *, timeout_seconds, on_text_update, provider=None):
        invoked_llms.append(llm)
        call_order.append("invoke")
        if len(invoked_llms) == 1:
            raise primary_exc
        return fallback_ai_msg

    async def fake_persist_response(attempt, *args, **kwargs):
        call_order.append(f"persist_response:{attempt}")

    async def fake_persist_error(attempt, *args, **kwargs):
        call_order.append(f"persist_error:{attempt}")

    monkeypatch.setattr(
        "sreda.runtime.llm_caller.ainvoke_with_streaming_timeout", fake_invoke,
    )
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", MagicMock(), raising=False,
    )

    caller = _make_caller(fallback_llm=fallback_llm)
    result = _run(caller.ainvoke_with_fallback(
        messages=[],
        iter_n=3,
        trace_meta=trace_meta,
        on_text_update=None,
        persist_request=fake_persist_request,
        persist_response=fake_persist_response,
        persist_error=fake_persist_error,
    ))

    assert result is fallback_ai_msg
    # fallback invoke targeted the fallback llm (second invoke)
    assert invoked_llms[1] is fallback_llm
    # ordering: primary req → primary invoke → primary err → fallback req →
    # fallback invoke → fallback response
    assert call_order == [
        "persist_request:primary",
        "invoke",
        "persist_error:primary",
        "persist_request:fallback",
        "invoke",
        "persist_response:fallback",
    ], call_order


# ---------------------------------------------------------------------------
# (g) An alert-send failure must NOT mask the original LLM error (the alert
#     try/except swallows the alert exception; the primary exception still
#     propagates when there is no fallback). Codex high #5 R1 optional gap.
# ---------------------------------------------------------------------------

def test_alert_send_failure_does_not_mask_original_error(monkeypatch):
    primary_exc = ValueError("original boom")
    persist_request, persist_response, persist_error = _make_callables()

    monkeypatch.setattr(
        "sreda.runtime.llm_caller.ainvoke_with_streaming_timeout",
        AsyncMock(side_effect=primary_exc),
    )

    def _boom_alert(*args, **kwargs):
        raise RuntimeError("alert backend down")

    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", _boom_alert, raising=False,
    )

    caller = _make_caller(fallback_llm=None)
    # The ORIGINAL ValueError propagates, NOT the alert's RuntimeError.
    with pytest.raises(ValueError, match="original boom"):
        _run(caller.ainvoke_with_fallback(
            messages=[],
            iter_n=0,
            trace_meta={},
            on_text_update=None,
            persist_request=persist_request,
            persist_response=persist_response,
            persist_error=persist_error,
        ))
    persist_error.assert_awaited_once()
