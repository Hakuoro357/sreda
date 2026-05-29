"""Tests for runtime/planner/orchestrator.py — Sub-A12 Phase B.4.

Covers the full lifecycle: insert_pending → call_planner → mark_received →
parse/validate/compile → mark_valid OR mark_invalid + retry.

Persistence tests use the standard ``db_session`` fixture (SQLite in-mem
via conftest). LLM is always mocked — orchestrator must work without a
real provider.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from sreda.runtime.planner.llm import (
    PlannerCallResult,
    PlannerProviderUnavailable,
    PlannerTimeoutError,
)
from sreda.runtime.planner.orchestrator import (
    PlannerContext,
    _redact_for_alert,
    run,
)
from sreda.runtime.planner.prompt_builder import (
    NowMoment,
    ProfileSnapshot,
)
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx(*, user_message: str = "купи молоко") -> PlannerContext:
    """Realistic PlannerContext for happy-path tests."""
    return PlannerContext(
        tenant_id="tenant_test_001",
        run_id="run_test_001",
        feature_key="housewife_assistant",
        user_message=user_message,
        voice_meta=None,
        now=NowMoment(datetime(2026, 5, 28, 14, 30)),
        profile=ProfileSnapshot(name="Тест"),
        memories=(),
        active_turn=None,
        closed_turns=(),
        available_tools=tuple(MIGRATED_TOOL_SPECS),
        composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
        composer_llm_prompt_keys=(),
        composer_registry_snapshot_hash=COMPOSER_REGISTRY.snapshot_hash(),
        tool_registry_version="v1",
        few_shot_block="",
        planner_provider="mimo-v2.5",
    )


def _make_session_factory(db_session: Session):
    """Wrap a single db_session as a callable factory.

    Each call returns the SAME session (so persistence stays in the
    test's transaction scope). For test purposes context-manager
    semantics don't need real per-call session — just don't close.
    """
    class _SessionProxy:
        def __init__(self, session: Session) -> None:
            self._session = session

        def __enter__(self) -> Session:
            return self._session

        def __exit__(self, *exc_info: Any) -> None:
            # Don't close — fixture owns lifecycle. Just commit savepoint
            # if any (begin_nested may have been used by orchestrator).
            pass

    def factory():
        return _SessionProxy(db_session)

    return factory


def _make_valid_plan_payload() -> dict:
    """Minimal valid Plan JSON the orchestrator can pydantic-validate
    + run through compile()."""
    return {
        "schema_version": 1,
        "turn_classification": {
            "is_new_turn": True,
            "reason": "test plan",
        },
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                "args": {"items": [{"title": "молоко"}]},
                "expected_outcomes": [
                    {
                        "match": {"status": "added"},
                        "next": None,
                        "compose": {
                            "kind": "template",
                            "template_id": "shopping_added_ok",
                            "template_data": {"items": ["молоко"]},
                        },
                    },
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["молоко"]},
        },
    }


def _make_call_result(raw: str, *, attempt: int = 1) -> PlannerCallResult:
    return PlannerCallResult(
        raw_text=raw,
        latency_ms=100,
        provider="mimo-v2.5",
        model="mimo-v2.5-pro",
        attempt_no=attempt,
        parsed_plan=None,
    )


def _row(db_session: Session, eid: str) -> dict:
    """Read planner_executions row as plain dict for assertions."""
    result = db_session.execute(
        text("SELECT * FROM planner_executions WHERE id = :id"),
        {"id": eid},
    ).mappings().first()
    return dict(result) if result else {}


def _run(coro):
    """Sync wrapper for async tests."""
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutine(coro) else asyncio.run(coro)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_orchestrator_happy_path_returns_success(db_session: Session) -> None:
    """Single-attempt success: valid plan returned → PlannerResult(success=True)
    with plan + execution_plan populated."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result(json.dumps(_make_valid_plan_payload()))

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))

    assert result.success
    assert result.final_attempt_no == 1
    assert result.plan is not None
    assert result.execution_plan is not None
    assert result.error_summary is None
    # ExecutionPlan should have one layer with s1
    assert result.execution_plan.layers == (("s1",),)


def test_orchestrator_happy_path_persists_valid_status(db_session: Session) -> None:
    """Success path lands in planner_executions with status='valid' and
    plan_json populated."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result(json.dumps(_make_valid_plan_payload()))

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))
    db_session.commit()  # ensure proxy-committed data is visible

    row = _row(db_session, result.execution_id)
    assert row["planner_status"] == "valid"
    assert row["plan_json"] is not None
    assert row["execution_plan_json"] is not None
    # SQLite stores bool as int 1/0; cast for portability
    assert bool(row["is_new_turn"]) is True


def test_orchestrator_records_latency_and_provider(db_session: Session) -> None:
    """planner_latency_ms + planner_provider + planner_model columns
    populated from PlannerCallResult.

    Codex B.4 R1 MAJOR: earlier code wrote `planner_model=""` and never
    updated it; now mark_received writes the actual resolved model."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return PlannerCallResult(
            raw_text=json.dumps(_make_valid_plan_payload()),
            latency_ms=234,
            provider="mimo-v2.5",
            model="mimo-v2.5-pro",
            attempt_no=1,
        )

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))
    db_session.commit()
    row = _row(db_session, result.execution_id)
    assert row["planner_provider"] == "mimo-v2.5"
    assert row["planner_model"] == "mimo-v2.5-pro"
    assert row["planner_latency_ms"] == 234


def test_orchestrator_persists_model_resolved_from_registry(db_session: Session) -> None:
    """When fake LLM returns a different model than provider key, the
    actual model from PlannerCallResult.model lands in DB (not just the
    provider key)."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return PlannerCallResult(
            raw_text=json.dumps(_make_valid_plan_payload()),
            latency_ms=50,
            provider="some-fallback-key",  # different from ctx setting
            model="actually-resolved-model-name",
            attempt_no=1,
        )

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))
    db_session.commit()
    row = _row(db_session, result.execution_id)
    assert row["planner_provider"] == "some-fallback-key"
    assert row["planner_model"] == "actually-resolved-model-name"


# ---------------------------------------------------------------------------
# Retry path
# ---------------------------------------------------------------------------


def test_orchestrator_retries_on_invalid_json_then_succeeds(db_session: Session) -> None:
    """Attempt 1: garbage JSON. Attempt 2: valid plan. Should succeed."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)
    calls: list[int] = []

    def fake_call(prompt: str, *, attempt_no: int = 1, **_kw: Any) -> PlannerCallResult:
        calls.append(attempt_no)
        if attempt_no == 1:
            return _make_call_result("not even json", attempt=1)
        return _make_call_result(json.dumps(_make_valid_plan_payload()), attempt=2)

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))

    assert result.success
    assert result.final_attempt_no == 2
    assert calls == [1, 2]
    assert len(result.raw_responses) == 2


def test_orchestrator_gives_up_after_two_invalid(db_session: Session) -> None:
    """Both attempts return garbage JSON → success=False with
    error_summary='invalid_plan_after_retry'."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result("definitely not json")

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))
    db_session.commit()

    assert not result.success
    assert result.final_attempt_no == 2
    assert result.error_summary == "invalid_plan_after_retry"
    assert len(result.raw_responses) == 2

    row = _row(db_session, result.execution_id)
    assert row["planner_status"] == "invalid"
    assert "json_decode_error" in row["validation_errors"]


def test_orchestrator_no_retry_when_invalid_retry_disabled(db_session: Session) -> None:
    """invalid_retry_enabled=False → single attempt, no retry."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)
    calls: list[int] = []

    def fake_call(prompt: str, *, attempt_no: int = 1, **_kw: Any) -> PlannerCallResult:
        calls.append(attempt_no)
        return _make_call_result("garbage", attempt=attempt_no)

    result = asyncio.run(run(
        ctx, session_factory=factory, call_planner_fn=fake_call,
        invalid_retry_enabled=False,
    ))
    assert not result.success
    assert result.final_attempt_no == 1
    assert calls == [1]


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


def test_orchestrator_provider_unavailable_no_retry(db_session: Session) -> None:
    """PlannerProviderUnavailable on attempt 1 → immediate failure, no retry.
    (Retry won't help if no provider key exists.)"""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)
    calls: list[int] = []

    def fake_call(prompt: str, *, attempt_no: int = 1, **_kw: Any) -> PlannerCallResult:
        calls.append(attempt_no)
        raise PlannerProviderUnavailable("no key")

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))
    assert not result.success
    assert result.error_summary == "provider_unavailable"
    assert calls == [1]


def test_orchestrator_timeout_no_retry(db_session: Session) -> None:
    """Timeout in LLM call → immediate failure."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        raise PlannerTimeoutError("timed out")

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))
    assert not result.success
    assert result.error_summary == "timeout"


# ---------------------------------------------------------------------------
# Validator / compiler errors → retry path
# ---------------------------------------------------------------------------


def test_orchestrator_invalid_tool_triggers_retry_then_fails(
    db_session: Session,
) -> None:
    """Unknown tool name → validator violations → retry → still fails."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    def make_bad_plan(invented_tool: str) -> dict:
        p = _make_valid_plan_payload()
        p["actions"]["s1"]["tool"] = invented_tool
        return p

    def fake_call(prompt: str, *, attempt_no: int = 1, **_kw: Any) -> PlannerCallResult:
        return _make_call_result(json.dumps(make_bad_plan("totally_fake_tool")),
                                 attempt=attempt_no)

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))
    assert not result.success
    assert result.final_attempt_no == 2
    assert "invalid_plan_after_retry" == result.error_summary


# ---------------------------------------------------------------------------
# Clarification mode (empty actions)
# ---------------------------------------------------------------------------


def test_orchestrator_clarification_plan_succeeds_with_empty_layers(
    db_session: Session,
) -> None:
    """A valid clarity='needs_clarification' plan with empty actions
    compiles to ExecutionPlan(layers=())."""
    ctx = _make_ctx(user_message="напомни купить хлеб")
    factory = _make_session_factory(db_session)

    clarification_payload = {
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "no time"},
        "clarity": "needs_clarification",
        "clarity_reason": "не указано время",
        "actions": {},
        "compose": {
            "kind": "template",
            "template_id": "ask_when_to_remind",
            "template_data": {"what": "купить хлеб"},
        },
    }

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result(json.dumps(clarification_payload))

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))
    assert result.success
    assert result.execution_plan.layers == ()
    assert result.plan.clarity == "needs_clarification"


# ---------------------------------------------------------------------------
# Persistence: insert_pending populates required columns
# ---------------------------------------------------------------------------


def test_orchestrator_insert_pending_populates_required_columns(
    db_session: Session,
) -> None:
    """Stage 1 INSERT writes tenant_id/feature_key/provider/registry
    snapshot columns."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result(json.dumps(_make_valid_plan_payload()))

    result = asyncio.run(run(ctx, session_factory=factory, call_planner_fn=fake_call))
    db_session.commit()
    row = _row(db_session, result.execution_id)
    assert row["tenant_id"] == ctx.tenant_id
    assert row["feature_key"] == ctx.feature_key
    assert row["run_id"] == ctx.run_id
    assert row["composer_registry_snapshot_hash"] == ctx.composer_registry_snapshot_hash
    assert row["tool_registry_version"] == ctx.tool_registry_version


# ---------------------------------------------------------------------------
# Admin alert + redaction
# ---------------------------------------------------------------------------


def test_orchestrator_calls_alert_on_final_failure(db_session: Session) -> None:
    """Failed plan after retry → admin_alert_fn called with redacted body."""
    ctx = _make_ctx(user_message="купи молоко тел +79991234567")
    factory = _make_session_factory(db_session)
    alerts: list[dict] = []

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result("garbage")

    def fake_alert(*, severity: str, title: str, body: str, **kw: Any) -> None:
        alerts.append({"severity": severity, "title": title, "body": body, **kw})

    result = asyncio.run(run(
        ctx, session_factory=factory, call_planner_fn=fake_call,
        admin_alert_fn=fake_alert,
    ))
    assert not result.success
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["severity"] == "P1"
    assert result.execution_id in alert["body"]
    # PII redaction: phone replaced
    assert "+79991234567" not in alert["body"]
    assert "[PHONE]" in alert["body"]


def test_orchestrator_alerts_on_persistence_failure(db_session: Session) -> None:
    """Codex B.4 R2 MEDIUM: persistence terminal failures now also call
    _maybe_alert(). Simulate insert_pending failure by injecting a
    session_factory that raises on enter."""
    ctx = _make_ctx()
    alerts: list[dict] = []

    class _BadFactoryProxy:
        def __enter__(self):
            raise RuntimeError("simulated DB outage at insert_pending")

        def __exit__(self, *a):
            return False

    def factory():
        return _BadFactoryProxy()

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result(json.dumps(_make_valid_plan_payload()))

    def fake_alert(*, severity: str, title: str, body: str, **kw: Any) -> None:
        alerts.append({"severity": severity, "title": title, "body": body, **kw})

    result = asyncio.run(run(
        ctx, session_factory=factory, call_planner_fn=fake_call,
        admin_alert_fn=fake_alert,
    ))
    assert not result.success
    assert "insert_pending" in result.error_summary
    # Persistence failure must surface as admin alert too
    assert len(alerts) == 1
    assert "persistence:insert_pending" in alerts[0]["body"]


def test_orchestrator_no_alert_when_admin_alert_fn_is_none(
    db_session: Session,
) -> None:
    """admin_alert_fn=None → no alerts (offline/eval mode)."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result("garbage")

    # Should not raise
    result = asyncio.run(run(
        ctx, session_factory=factory, call_planner_fn=fake_call,
        admin_alert_fn=None,
    ))
    assert not result.success


# ---------------------------------------------------------------------------
# Redaction helper (unit)
# ---------------------------------------------------------------------------


def test_orchestrator_ephemeral_mode_skips_persistence(db_session: Session) -> None:
    """Codex B.7 R1 MAJOR fix: session_factory=None → no DB writes.
    Used by live eval script which lacks a real agent_runs FK target.

    Verify: success path runs end-to-end, returns plan + execution_plan,
    but no row appears in planner_executions."""
    ctx = _make_ctx()

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result(json.dumps(_make_valid_plan_payload()))

    result = asyncio.run(run(
        ctx, session_factory=None, call_planner_fn=fake_call,
    ))

    assert result.success
    assert result.plan is not None
    assert result.execution_plan is not None

    # No row written
    row = _row(db_session, result.execution_id)
    assert row == {}


def test_redact_phone_ru() -> None:
    assert "[PHONE]" in _redact_for_alert("позвони +79991234567")
    assert "+79991234567" not in _redact_for_alert("позвони +79991234567")


def test_redact_phone_with_spaces_and_dashes() -> None:
    """Codex B.4 R1 MEDIUM: phones with spaces/dashes/parens must
    also be redacted, not just compact digit-strings."""
    for raw in (
        "позвони +7 999 123 45 67",
        "тел +7-999-123-45-67",
        "+7 (999) 123-45-67",
        "8 999 1234567",
    ):
        out = _redact_for_alert(raw)
        assert "[PHONE]" in out, f"{raw!r} not redacted: {out!r}"
        assert "999" not in out, f"{raw!r} leaked digits: {out!r}"


def test_redact_email() -> None:
    assert "[EMAIL]" in _redact_for_alert("write to test@example.com")


def test_redact_email_with_dots_plus_and_multi_tld() -> None:
    """Codex B.4 R1 MEDIUM: realistic email forms (first.last+tag@
    sub.domain.co.uk) must be redacted."""
    for raw in (
        "first.last@example.com",
        "user+tag@example.org",
        "first.last+tag@sub.example.co.uk",
        "boris-test@example-mail.io",
    ):
        out = _redact_for_alert(raw)
        assert "[EMAIL]" in out
        assert "@" not in out or out.count("@") == 0, f"{raw!r} leaked: {out!r}"


def test_redact_username() -> None:
    assert "[USERNAME]" in _redact_for_alert("@boris_test wrote")


def test_redact_caps_length() -> None:
    assert len(_redact_for_alert("x" * 1000, max_chars=200)) == 200


# ---------------------------------------------------------------------------
# Sub-A12 D.2-enable R2 — non-bypassable composer-LLM gate
# ---------------------------------------------------------------------------


def _llm_plan_payload() -> dict:
    """Plan whose ROOT compose is kind='llm' recipe_narrative with all
    required template_data present (so only the gate, not required-keys,
    decides acceptance)."""
    return {
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                "args": {"items": [{"title": "молоко"}]},
                "expected_outcomes": [
                    {"match": {"status": "added"}, "next": None},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "llm",
            "llm_prompt_key": "recipe_narrative",
            "template_data": {
                "recipe_title": "борщ",
                "ingredients": ["свёкла", "капуста"],
                "steps": ["варить"],
                "source": "generated",
            },
        },
    }


def _settings_stub(enabled_keys: set[str]):
    from types import SimpleNamespace
    return lambda: SimpleNamespace(
        composer_llm_enabled_keys=frozenset(enabled_keys)
    )


def _ctx_proposing_llm_keys(keys: tuple[str, ...]):
    from sreda.runtime.planner.orchestrator import PlannerContext
    base = _make_ctx()
    return PlannerContext(**{**base.__dict__, "composer_llm_prompt_keys": keys})


def test_llm_compose_rejected_when_key_not_enabled(db_session: Session) -> None:
    """Gate: settings enable NO keys → even though the caller proposes
    recipe_narrative and the planner emits kind='llm', effective keys = ()
    → validate_plan rejects it → invalid_plan (non-bypassable)."""
    ctx = _ctx_proposing_llm_keys(("recipe_narrative",))
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return PlannerCallResult(
            raw_text=json.dumps(_llm_plan_payload(), ensure_ascii=False),
            latency_ms=1, provider="p", model="m", attempt_no=1,
        )

    result = asyncio.run(run(
        ctx, session_factory=factory, call_planner_fn=fake_call,
        invalid_retry_enabled=False,
        settings_factory=_settings_stub(set()),  # no keys enabled
    ))
    db_session.commit()
    assert result.success is False
    assert result.error_summary == "invalid_plan_after_retry"


def test_llm_compose_accepted_when_key_enabled(db_session: Session) -> None:
    """Gate: settings enable recipe_narrative + caller proposes it +
    required data present → effective includes it → plan validates."""
    ctx = _ctx_proposing_llm_keys(("recipe_narrative",))
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return PlannerCallResult(
            raw_text=json.dumps(_llm_plan_payload(), ensure_ascii=False),
            latency_ms=1, provider="p", model="m", attempt_no=1,
        )

    result = asyncio.run(run(
        ctx, session_factory=factory, call_planner_fn=fake_call,
        invalid_retry_enabled=False,
        settings_factory=_settings_stub({"recipe_narrative"}),
    ))
    db_session.commit()
    assert result.success is True, (
        f"expected success, got error={result.error_summary!r}"
    )


def test_llm_gate_drops_stale_key_not_in_registry(db_session: Session) -> None:
    """Even if settings AND caller name a key absent from the registry,
    it's dropped (effective = ∩ registry) → kind='llm' using it rejected.
    Here the caller proposes a ghost key; the plan uses recipe_narrative
    which settings did NOT enable → rejected."""
    ctx = _ctx_proposing_llm_keys(("ghost_key", "recipe_narrative"))
    factory = _make_session_factory(db_session)

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return PlannerCallResult(
            raw_text=json.dumps(_llm_plan_payload(), ensure_ascii=False),
            latency_ms=1, provider="p", model="m", attempt_no=1,
        )

    result = asyncio.run(run(
        ctx, session_factory=factory, call_planner_fn=fake_call,
        invalid_retry_enabled=False,
        settings_factory=_settings_stub({"ghost_key"}),  # ghost not in registry
    ))
    db_session.commit()
    assert result.success is False
