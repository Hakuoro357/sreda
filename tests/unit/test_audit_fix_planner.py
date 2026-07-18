"""Регрессионные тесты фиксов аудита 2026-07-18 — домен planner.

Покрывает (по находкам planner-core / planner-exec / cross-concurrency):

1. MAJOR planner-core #1: retry-фидбек живёт ОТДЕЛЬНОЙ секцией суффикса,
   а не внутри capped user_message — длинное сообщение (>2953 символов)
   получает вторую попытку вместо терминального PromptBudgetExceeded.
2. MINOR planner-core #2: ts с таймзоной рендерится как HH:MM, а не мусор
   вида ``59+03``.
3. MINOR planner-core #3: заголовок алерта отражает реальный класс сбоя.
4. MINOR planner-core #4: raw-ответ попытки 1 не перезаписывается попыткой 2.
5. MINOR planner-core #5: multi-ref items валидатор ловит чистым сообщением.
6. MINOR planner-core #7: дубликаты match среди веток отклоняются.
7. MINOR planner-core #9: _trim никогда не возвращает строку длиннее бюджета.
8. MAJOR planner-exec #1: settle-окно для ledger-статуса ``started``.
9. MINOR planner-exec #2: падение ledger open_step не роняет весь gather.
10. MINOR planner-exec #5: error_summary без repr пользовательских данных.
11. MINOR planner-exec #6: poison-pill потолок recovery_attempt.
12. MINOR planner-exec #7: терминальный статус ledger не перезаписывается.

Без сети; PG не нужен (SQLite StaticPool по образцу test_recovery_scanner.py).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — регистрация всех ORM-таблиц
import sreda.db.models.planner  # noqa: F401
import sreda.db.models.audit_feed  # noqa: F401

from sreda.db.models import AgentRun, AgentThread, PlannerExecution, Tenant, Workspace
from sreda.runtime.planner.executor import execute_plan
from sreda.runtime.planner.interpolation import InvalidReferenceError, resolve_refs
from sreda.runtime.planner.llm import PlannerCallResult
from sreda.runtime.planner.orchestrator import (
    PlannerContext,
    _maybe_alert,
    run,
)
from sreda.runtime.planner.persistence import read_planner_pii
from sreda.runtime.planner.plan_compiler import compile as compile_plan
from sreda.runtime.planner.prompt_builder import (
    NowMoment,
    ProfileSnapshot,
    TurnMessage,
    TurnSnapshot,
    _trim,
    build_variable_suffix,
)
from sreda.runtime.planner.recovery import (
    TERMINAL_FAILED_NEEDS_MANUAL,
    decide_recovery,
)
from sreda.runtime.planner.recovery_scanner import (
    MAX_RECOVERY_ATTEMPTS,
    run_recovery_scan,
)
from sreda.runtime.planner.schemas import (
    Action,
    ComposerCall,
    OutcomeBranch,
    Plan,
    TurnClassification,
)
from sreda.runtime.planner.step_ledger import mark_step_status, open_step
from sreda.runtime.planner.validator import validate_plan
from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS


REGISTRY = {s.name: s for s in MIGRATED_TOOL_SPECS}
NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
SETTLE_SECONDS = 240


# ---------------------------------------------------------------------------
# Общие хелперы (по образцу test_planner_orchestrator.py)
# ---------------------------------------------------------------------------


def _make_ctx(*, user_message: str = "купи молоко") -> PlannerContext:
    return PlannerContext(
        tenant_id="tenant_test_001",
        run_id="run_test_001",
        feature_key="housewife_assistant",
        user_message=user_message,
        voice_meta=None,
        now=NowMoment(datetime(2026, 7, 18, 14, 30)),
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


def _make_valid_plan_payload() -> dict:
    return {
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "test plan"},
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


def _settings_stub(enabled_keys: set[str] | None = None):
    return lambda: SimpleNamespace(
        composer_llm_enabled_keys=frozenset(enabled_keys or set())
    )


class _SessionProxy:
    """Оборачивает db_session как factory (контракт context manager'а)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *exc_info: Any) -> None:
        pass


def _make_session_factory(db_session: Session):
    return lambda: _SessionProxy(db_session)


# ---------------------------------------------------------------------------
# SQLite StaticPool engine для scanner/ledger/executor тестов
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Function-scoped: scan-тесты не должны видеть execution'ы соседних
    тестов (claim захватывает ВСЕ in_progress с истёкшим лизом)."""
    eng = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    factory = sessionmaker(bind=engine)
    sessions: list[Session] = []

    def _factory() -> Session:
        s = factory()
        sessions.append(s)
        return s

    yield _factory
    for s in sessions:
        try:
            s.close()
        except Exception:
            pass


def _seed_execution(
    session: Session,
    *,
    execution_status: str = "in_progress",
    recovery_lease_until: datetime | None = None,
    recovery_attempt: int = 0,
) -> str:
    tenant_id = f"t_{uuid4().hex[:8]}"
    ws_id = f"ws_{uuid4().hex[:8]}"
    thread_id = f"th_{uuid4().hex[:8]}"
    run_id = f"run_{uuid4().hex[:8]}"
    exec_id = f"pe_{uuid4().hex[:8]}"
    session.add(Tenant(id=tenant_id, name="test-tenant"))
    session.add(Workspace(id=ws_id, tenant_id=tenant_id, name="test-ws"))
    session.add(
        AgentThread(
            id=thread_id,
            tenant_id=tenant_id,
            workspace_id=ws_id,
            channel_type="telegram",
            external_chat_id="99",
        )
    )
    session.add(
        AgentRun(
            id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            workspace_id=ws_id,
            action_type="chat",
        )
    )
    session.add(
        PlannerExecution(
            id=exec_id,
            run_id=run_id,
            tenant_id=tenant_id,
            feature_key="housewife_assistant",
            planner_prompt_version=1,
            planner_provider="mimo-v2.5-pro",
            planner_model="mimo-v2.5-pro",
            planner_status="valid",
            execution_status=execution_status,
            execution_log_json=[],
            created_at=NOW,
            recovery_lease_until=recovery_lease_until,
            recovery_attempt=recovery_attempt,
        )
    )
    session.flush()
    session.commit()  # StaticPool: новые сессии видят только закоммиченное
    return exec_id


# ---------------------------------------------------------------------------
# 1. MAJOR planner-core #1 — retry-фидбек вне capped user_message
# ---------------------------------------------------------------------------


def test_long_message_gets_retry_with_separate_feedback_section() -> None:
    """user_message ~3500 символов (>4096-1143): раньше попытка 2 гарантированно
    падала с PromptBudgetExceeded. Теперь фидбек — отдельная fenced-секция,
    и ретрай доезжает до валидного плана."""
    long_message = " ".join(["надиктованный список покупок"] * 120)  # ~3480
    assert 2953 < len(long_message) <= 4096
    ctx = _make_ctx(user_message=long_message)

    prompts: list[str] = []
    bad_payload = _make_valid_plan_payload()
    bad_payload["compose"]["template_data"] = {"items": "${s1.items.only.title}"}

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        prompts.append(prompt)
        if len(prompts) == 1:
            return _make_call_result(json.dumps(bad_payload))
        return _make_call_result(json.dumps(_make_valid_plan_payload()))

    result = asyncio.run(
        run(
            ctx,
            session_factory=None,  # ephemeral: без DB
            call_planner_fn=fake_call,
            invalid_retry_enabled=True,
            settings_factory=_settings_stub(),
        )
    )

    assert result.success, f"ретрай длинного сообщения сломан: {result.error_summary}"
    assert len(prompts) == 2
    retry_prompt = prompts[1]
    # Фидбек — отдельная секция, а не хвост ТЕКУЩЕЕ_СООБЩЕНИЕ.
    assert "РЕТРАЙ_ФИДБЕК" in retry_prompt
    assert "[АВТОМАТИЧЕСКИЙ РЕТРАЙ]" in retry_prompt
    # Полное сообщение пользователя на месте (не обрезано ради фидбека).
    assert long_message in retry_prompt


def test_retry_feedback_omitted_gracefully_when_suffix_full() -> None:
    """Если суффикс забит обязательными секциями, фидбек обрезается/опускается,
    но НИКОГДА не становится причиной PromptBudgetExceeded."""
    from sreda.runtime.planner.prompt_builder import PromptBudget

    budget = PromptBudget(max_suffix_chars=1_200)
    suffix = build_variable_suffix(
        profile=ProfileSnapshot(name="Тест"),
        memories=[],
        active_turn=None,
        closed_turns=[],
        now=NowMoment(datetime(2026, 7, 18, 14, 30)),
        user_message="короткое",
        retry_feedback="[АВТОМАТИЧЕСКИЙ РЕТРАЙ]\n" + "x" * 1143,
        budget=budget,
    )
    assert len(suffix) <= budget.max_suffix_chars


# ---------------------------------------------------------------------------
# 2. MINOR planner-core #2 — корректный рендер ts с таймзоной
# ---------------------------------------------------------------------------


def test_turn_message_ts_tz_aware_renders_hh_mm() -> None:
    """Продюсер (planner_chat) пишет isoformat(timespec='seconds') с +03:00 —
    в промте должно быть [12:47], а не мусорное [59+03]."""
    turn = TurnSnapshot(
        turn_id="t1",
        started_at="2026-07-18T12:47:59+03:00",
        summary=None,
        is_active=True,
        messages=[
            TurnMessage(role="юзер", text="привет", ts="2026-07-18T12:47:59+03:00"),
            TurnMessage(role="среда", text="здравствуй", ts="2026-07-18T12:48:01+03:00"),
        ],
    )
    suffix = build_variable_suffix(
        profile=ProfileSnapshot(name="Тест"),
        memories=[],
        active_turn=turn,
        closed_turns=[],
        now=NowMoment(datetime(2026, 7, 18, 14, 30)),
        user_message="покажи дела",
    )
    assert "[12:47] юзер: привет" in suffix
    assert "[12:48] среда: здравствуй" in suffix
    assert "[59+03]" not in suffix
    assert "[01+03]" not in suffix


def test_turn_message_ts_naive_iso_still_renders_hh_mm() -> None:
    turn = TurnSnapshot(
        turn_id="t1",
        started_at="2026-07-18T12:47:59",
        summary=None,
        is_active=True,
        messages=[TurnMessage(role="юзер", text="привет", ts="2026-07-18T12:47:59")],
    )
    suffix = build_variable_suffix(
        profile=ProfileSnapshot(name="Тест"),
        memories=[],
        active_turn=turn,
        closed_turns=[],
        now=NowMoment(datetime(2026, 7, 18, 14, 30)),
        user_message="покажи дела",
    )
    assert "[12:47] юзер: привет" in suffix


# ---------------------------------------------------------------------------
# 3. MINOR planner-core #3 — честные заголовки алертов
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "errors, expected_title",
    [
        ("provider_unavailable: mimo down", "planner: provider_unavailable"),
        ("timeout: 60s", "planner: timeout"),
        ("prompt_budget_exceeded: x", "planner: prompt_budget_exceeded"),
        (
            "persistence:mark_received:OperationalError: boom",
            "planner: persistence:mark_received",
        ),
        ("validator_violations: ...", "planner: validator_violations"),
        ("llm_call:RuntimeError: boom", "planner: llm_call"),
    ],
)
def test_alert_title_reflects_failure_class(errors: str, expected_title: str) -> None:
    captured: list[dict] = []
    _maybe_alert(
        lambda **kw: captured.append(kw),
        "exec_123",
        _make_ctx(),
        errors=errors,
    )
    assert captured, "алерт не отправлен"
    assert captured[0]["title"] == expected_title


# ---------------------------------------------------------------------------
# 4. MINOR planner-core #4 — raw попытки 1 не перезаписывается
# ---------------------------------------------------------------------------


def test_mark_received_preserves_attempt1_raw(db_session: Session) -> None:
    """После ретрая raw_planner_response содержит ОБА ответа с маркерами
    попыток — post-mortem попытки 1 не теряется."""
    ctx = _make_ctx()
    factory = _make_session_factory(db_session)

    bad_payload = _make_valid_plan_payload()
    bad_payload["compose"]["template_data"] = {"items": "${s1.items.only.title}"}

    # Первый ответ — невалидный план (validator violation), второй — валидный.
    raws = iter([
        json.dumps(bad_payload),
        json.dumps(_make_valid_plan_payload()),
    ])

    def fake_call(prompt: str, **_kw: Any) -> PlannerCallResult:
        return _make_call_result(next(raws), attempt=2)

    result = asyncio.run(
        run(
            ctx,
            session_factory=factory,
            call_planner_fn=fake_call,
            invalid_retry_enabled=True,
            settings_factory=_settings_stub(),
        )
    )
    assert result.success
    assert result.final_attempt_no == 2

    row = db_session.get(PlannerExecution, result.execution_id)
    assert row is not None
    persisted = read_planner_pii(row, "raw_planner_response") or ""
    assert "=== planner attempt 1 ===" in persisted
    assert "=== planner attempt 2 ===" in persisted
    # Мусорный raw попытки 1 (с .only-ссылкой) сохранился.
    assert "s1.items.only.title" in persisted


# ---------------------------------------------------------------------------
# 5. MINOR planner-core #5 — multi-ref items: чистое нарушение
# ---------------------------------------------------------------------------


def test_validator_multi_ref_items_clean_violation() -> None:
    """items='${s1.items} ${s2.items}' — НЕ один ref: чистое сообщение про
    «одну ссылку», без мусорного поля 'items} ${s2'."""
    plan = Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="покажи"),
        actions={
            "s1": Action(
                tool="list_reminders", args={},
                expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
                depends_on=[],
            ),
            "s2": Action(
                tool="list_reminders", args={},
                expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
                depends_on=[],
            ),
        },
        compose=ComposerCall(
            kind="template",
            template_id="reminders_list_show",
            template_data={"items": "${s1.items} ${s2.items}"},
        ),
    )
    violations = validate_plan(plan, REGISTRY)
    mismatches = [v for v in violations if v.code == "show_template_source_mismatch"]
    assert mismatches, f"ожидалось show_template_source_mismatch: {[v.code for v in violations]}"
    msg = mismatches[0].message
    assert "ОДНУ ссылку" in msg
    # Старый мусорный разбор («field 'items} ${s2' на шаге s1») ушёл.
    assert "items} ${s2 (" not in msg
    assert mismatches[0].tool is None  # не инструмент target-шага


# ---------------------------------------------------------------------------
# 6. MINOR planner-core #7 — дубликаты match отклоняются
# ---------------------------------------------------------------------------


def test_validator_duplicate_branch_match_rejected() -> None:
    plan = Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="дубль"),
        actions={
            "s1": Action(
                tool="add_shopping_items",
                args={"items": [{"title": "x"}]},
                expected_outcomes=[
                    OutcomeBranch(match={"status": "added"}),
                    OutcomeBranch(match={"status": "added"}),
                ],
                depends_on=[],
            ),
        },
        compose=ComposerCall(
            kind="template",
            template_id="shopping_added_ok",
            template_data={"items": ["x"]},
        ),
    )
    codes = {v.code for v in validate_plan(plan, REGISTRY)}
    assert "duplicate_branch_match" in codes


def test_validator_distinct_branch_matches_pass() -> None:
    plan = Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="ок"),
        actions={
            "s1": Action(
                tool="add_shopping_items",
                args={"items": [{"title": "x"}]},
                expected_outcomes=[
                    OutcomeBranch(match={"status": "added"}),
                    OutcomeBranch(match={"status": "duplicate"}),
                    OutcomeBranch(match={}),
                ],
                depends_on=[],
            ),
        },
        compose=ComposerCall(
            kind="template",
            template_id="shopping_added_ok",
            template_data={"items": ["x"]},
        ),
    )
    codes = {v.code for v in validate_plan(plan, REGISTRY)}
    assert "duplicate_branch_match" not in codes


# ---------------------------------------------------------------------------
# 7. MINOR planner-core #9 — _trim держит контракт бюджета
# ---------------------------------------------------------------------------


def test_trim_never_returns_longer_than_budget() -> None:
    out = _trim("hello world", 2, marker="...[длинный маркер]...")
    assert len(out) <= 2
    out2 = _trim("hello world", 5)
    assert len(out2) <= 5
    assert _trim("hi", 10) == "hi"


# ---------------------------------------------------------------------------
# 8. MAJOR planner-exec #1 — settle-окно для started
# ---------------------------------------------------------------------------


def test_decide_recovery_started_respects_settle_window() -> None:
    """started + durable + probe==False: окно не истекло → re_probe (живой
    to_thread ещё может закоммитить); окно истекло → failed_needs_manual."""
    assert decide_recovery(
        ledger_status="started",
        is_durable_write=True,
        probed_committed=False,
        settle_window_elapsed=False,
    ) == "re_probe"
    assert decide_recovery(
        ledger_status="started",
        is_durable_write=True,
        probed_committed=False,
        settle_window_elapsed=True,
    ) == "mark_failed_needs_manual"


# ---------------------------------------------------------------------------
# 9. MINOR planner-exec #2 — падение open_step не роняет gather
# ---------------------------------------------------------------------------


class _StubTool:
    def __init__(self, name: str, return_raw: str):
        self.name = name
        self._return_raw = return_raw
        self.calls = 0

    async def ainvoke(self, args: dict) -> str:
        self.calls += 1
        return self._return_raw


def _exec_plan_two_steps() -> Plan:
    return Plan.model_validate({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "test"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                "args": {"items": [{"title": "x"}]},
                "expected_outcomes": [
                    {
                        "match": {"status": "added"},
                        "next": None,
                        "compose": {
                            "kind": "template",
                            "template_id": "shopping_added_ok",
                            "template_data": {"items": ["x"]},
                        },
                    },
                ],
                "intent_group": "default",
                "depends_on": [],
            },
            "s2": {
                "tool": "list_shopping",
                "args": {},
                "expected_outcomes": [
                    {
                        "match": {"status": "empty"},
                        "next": None,
                        "compose": {
                            "kind": "template",
                            "template_id": "shopping_list_empty",
                            "template_data": {},
                        },
                    },
                ],
                # отдельная группа: honest_partial скипает соседей по группе,
                # а нам нужно проверить выживание НЕЗАВИСИМОГО шага.
                "intent_group": "other",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["x"]},
        },
    })


def test_ledger_open_failure_does_not_crash_gather(
    session_factory, engine, monkeypatch
) -> None:
    """open_step падает для s1 → s1 получает StepResult(status='error',
    ledger_open_failed), инструмент НЕ запускается, а соседний s2 доезжает;
    execute_plan НЕ бросает исключение."""
    with Session(engine) as seed:
        exec_id = _seed_execution(seed)

    import sreda.runtime.planner.executor as executor_mod

    real_open_step = executor_mod.open_step

    def _flaky_open_step(session, *, step_id: str, **kwargs):
        if step_id == "s1":
            raise RuntimeError("ledger db down")
        return real_open_step(session, step_id=step_id, **kwargs)

    monkeypatch.setattr(executor_mod, "open_step", _flaky_open_step)

    plan = _exec_plan_two_steps()
    ep = compile_plan(plan, REGISTRY)
    tool_add = _StubTool(
        "add_shopping_items", "ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]"
    )
    tool_list = _StubTool("list_shopping", "no shopping items")

    log = asyncio.run(
        execute_plan(
            ep,
            tools_by_name={
                "add_shopping_items": tool_add,
                "list_shopping": tool_list,
            },
            registry=REGISTRY,
            execution_id=exec_id,
            turn_key=f"turn_{uuid4().hex[:12]}",
            tenant_id="tenant_test",
            ledger_session_factory=session_factory,
            now_fn=lambda: NOW,
        )
    )

    by_step = {s.step_id: s for s in log.steps}
    assert by_step["s1"].status == "error"
    assert by_step["s1"].error_summary.startswith("ledger_open_failed:")
    assert tool_add.calls == 0, "инструмент не должен запускаться без ledger-строки"
    assert by_step["s2"].status == "ok"
    assert tool_list.calls == 1


# ---------------------------------------------------------------------------
# 10. MINOR planner-exec #5 — нет repr пользовательских данных в error_summary
# ---------------------------------------------------------------------------


def test_interpolation_error_has_no_value_repr() -> None:
    with pytest.raises(InvalidReferenceError) as exc_info:
        resolve_refs(
            {"arg": "${s1.secret_field.deeper}"},
            {"s1": {"secret_field": 42}},
        )
    msg = str(exc_info.value)
    assert "int" in msg
    assert "(value:" not in msg
    assert "42" not in msg


def test_arg_violation_summary_has_no_input_values() -> None:
    """pydantic errors() без include_input — сырые аргументы (PII) не должны
    попадать в error_summary."""
    plan = Plan.model_validate({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "test"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                # PII-маркер в невалидном аргументе (строка вместо списка).
                "args": {"items": "секрет-строка-pii-12345"},
                "expected_outcomes": [
                    {
                        "match": {"status": "added"},
                        "next": None,
                        "compose": {
                            "kind": "template",
                            "template_id": "shopping_added_ok",
                            "template_data": {"items": ["x"]},
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
            "template_data": {"items": ["x"]},
        },
    })
    ep = compile_plan(plan, REGISTRY)
    tool = _StubTool(
        "add_shopping_items", "ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]"
    )
    log = asyncio.run(
        execute_plan(
            ep,
            tools_by_name={"add_shopping_items": tool},
            registry=REGISTRY,
        )
    )
    step = log.steps[0]
    assert step.status == "arg_violation"
    assert "секрет-строка-pii-12345" not in (step.error_summary or "")


# ---------------------------------------------------------------------------
# 11. MINOR planner-exec #6 — poison-pill потолок recovery_attempt
# ---------------------------------------------------------------------------


def test_recovery_scan_poison_pill_terminalizes(
    session_factory, engine, monkeypatch
) -> None:
    """recover_execution падает детерминированно + recovery_attempt на
    потолке → execution терминализируется failed_needs_manual, лиз снят,
    алерт отправлен (а не вечный error-лог)."""
    with Session(engine) as seed:
        exec_id = _seed_execution(
            seed,
            execution_status="in_progress",
            recovery_lease_until=NOW - timedelta(seconds=10),  # лиз истёк
            recovery_attempt=MAX_RECOVERY_ATTEMPTS,
        )

    import sreda.runtime.planner.recovery_scanner as scanner_mod

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("deterministic recovery failure")

    monkeypatch.setattr(scanner_mod, "recover_execution", _boom)

    alerts: list[dict] = []
    summary = run_recovery_scan(
        session_factory,
        worker_id="scanner-test",
        now_fn=lambda: NOW,
        lease_seconds=300,
        settle_window_seconds=SETTLE_SECONDS,
        registry=REGISTRY,
        alert_fn=lambda **kw: alerts.append(kw),
    )

    assert summary["errored"] == 1
    with Session(engine) as check:
        execution = check.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.execution_status == TERMINAL_FAILED_NEEDS_MANUAL
        assert execution.recovery_lease_until is None  # больше не ре-клеймится
    assert alerts, "poison-эскалация обязана алертить"
    assert "poison" in alerts[0]["title"]


def test_recovery_scan_below_ceiling_not_terminalized(
    session_factory, engine, monkeypatch
) -> None:
    """recovery_attempt ниже потолка → execution остаётся in_progress
    (повторная попытка на следующем тикe)."""
    with Session(engine) as seed:
        exec_id = _seed_execution(
            seed,
            execution_status="in_progress",
            recovery_lease_until=NOW - timedelta(seconds=10),
            recovery_attempt=1,
        )

    import sreda.runtime.planner.recovery_scanner as scanner_mod

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("transient?")

    monkeypatch.setattr(scanner_mod, "recover_execution", _boom)

    summary = run_recovery_scan(
        session_factory,
        worker_id="scanner-test",
        now_fn=lambda: NOW,
        lease_seconds=300,
        settle_window_seconds=SETTLE_SECONDS,
        registry=REGISTRY,
    )
    assert summary["errored"] == 1
    with Session(engine) as check:
        execution = check.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.execution_status == "in_progress"


# ---------------------------------------------------------------------------
# 12. MINOR planner-exec #7 — терминальный статус не перезаписывается
# ---------------------------------------------------------------------------


def test_step_ledger_terminal_status_not_overwritten(engine) -> None:
    with Session(engine) as s:
        exec_id = _seed_execution(s)
        open_step(
            s,
            execution_id=exec_id,
            step_id="s1",
            tool="add_shopping_items",
            operation_id=uuid4().hex,
            now=NOW,
        )
        mark_step_status(
            s, execution_id=exec_id, step_id="s1", status="committed", now=NOW,
        )
        # Второй писатель пытается откатить committed → unknown: отказ.
        row = mark_step_status(
            s,
            execution_id=exec_id,
            step_id="s1",
            status="unknown",
            now=NOW + timedelta(seconds=5),
        )
        assert row.status == "committed"
        # Тот же статус повторно — идемпотентный no-op, не ошибка.
        row2 = mark_step_status(
            s,
            execution_id=exec_id,
            step_id="s1",
            status="committed",
            now=NOW + timedelta(seconds=6),
        )
        assert row2.status == "committed"
