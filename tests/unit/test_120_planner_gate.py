"""#120 — развилка планового контура в чате (red-before-impl).

Тесты НАЗВАНЫ в чеклисте приёмки #120 (утверждён владельцем 2026-06-10):
- п.1: гейченный тенант отвечает через плановый контур, легаси не зовётся;
- п.2: сбой планового пути → честный шаблон пользователю + алерт владельцу,
  легаси НЕ вызывается;
- п.3: чужой тенант — байт-в-байт легаси (гейт не меняет поведение).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage

# тестовая обвязка чата — из соседнего модуля (pytest кладёт tests/unit в path)
from test_conversation_chat import (
    ConstantEmbeddingClient,
    FakeLLM,
    FakeTelegram,
    _bootstrap,
    _chat_envelope,
)

from sreda.config.settings import get_settings
from sreda.runtime.executor import ActionRuntimeService


def _run_turn(monkeypatch, tmp_path: Path, name: str, *, gate: str,
              scripted_reply: str = "ЛЕГАСИ ОТВЕТ"):
    monkeypatch.setenv("SREDA_PLANNER_ENABLED_TENANTS", gate)
    session = _bootstrap(monkeypatch, tmp_path, name)
    get_settings.cache_clear()
    fake_llm = FakeLLM([AIMessage(content=scripted_reply)])
    telegram = FakeTelegram()
    svc = ActionRuntimeService(
        session,
        telegram_client=telegram,
        llm_client=fake_llm,
        embedding_client=ConstantEmbeddingClient(),
    )
    queued = svc.enqueue_action(_chat_envelope("добавь молоко"))
    asyncio.run(svc.process_job(queued.job_id))
    return session, telegram, fake_llm


def test_non_gated_tenant_uses_legacy(monkeypatch, tmp_path: Path):
    """Чеклист #120 п.3: чужой тенант — байт-в-байт легаси."""
    session, telegram, fake_llm = _run_turn(
        monkeypatch, tmp_path, f"g3_{uuid4().hex[:6]}.db",
        gate="tenant_someone_else",
    )
    try:
        assert telegram.sent and telegram.sent[0]["text"] == "ЛЕГАСИ ОТВЕТ"
        assert fake_llm._bound.calls, "легаси-LLM обязан был отработать"
    finally:
        session.close()


def test_gated_tenant_routes_to_planner(monkeypatch, tmp_path: Path):
    """Чеклист #120 п.1: тенант из гейта отвечает плановым контуром."""
    async def stub_loop(*, session, action, pf, context):
        from sreda.runtime.handlers import ChatLoopResult
        final = AIMessage(content="ПЛАНОВЫЙ ОТВЕТ")
        return ChatLoopResult(
            final_ai=final, messages=[*pf.messages, final],
            turn_msg_start_idx=pf._turn_msg_start_idx,
            called_tools=set(), hallucination_nudged=False,
            turn_timed_out=False, successful_tool_counts={},
            onboarding_resolution_called=False,
        )

    import sreda.runtime.planner_chat as pc
    monkeypatch.setattr(pc, "run_planner_chat_loop", stub_loop)
    session, telegram, fake_llm = _run_turn(
        monkeypatch, tmp_path, f"g1_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent and telegram.sent[0]["text"] == "ПЛАНОВЫЙ ОТВЕТ"
        assert not fake_llm._bound.calls, "легаси-цикл не должен вызываться"
    finally:
        session.close()


def test_planner_failure_honest_reply_and_alert_no_legacy(monkeypatch, tmp_path: Path):
    """Чеклист #120 п.2: сбой → честный шаблон + алерт владельцу, БЕЗ легаси."""
    alerts: list[tuple] = []

    def fake_alert(severity, title, body, **kw):
        alerts.append((severity, title, body))

    import sreda.runtime.planner_chat as pc
    import sreda.runtime.planner.orchestrator as orch
    monkeypatch.setattr(pc, "send_admin_alert", fake_alert)

    async def boom(*a, **kw):
        raise RuntimeError("искусственный сбой стадии плана")

    monkeypatch.setattr(orch, "run", boom)

    session, telegram, fake_llm = _run_turn(
        monkeypatch, tmp_path, f"g2_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent, "пользователь обязан получить ответ"
        text = telegram.sent[0]["text"]
        from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL
        assert text in BREAKDOWN_POOL, text  # «поломка» из пула (Борис 2026-06-11)
        assert not fake_llm._bound.calls, "отката в легаси быть не должно"
        assert alerts and "план" in alerts[0][1], alerts
        assert "t1" in alerts[0][2]
    finally:
        session.close()


def test_boundary_crash_canned_reply_and_alert(monkeypatch, tmp_path: Path):
    """Codex #120 R1 MAJOR: сбой на самой границе ветки (до входа в модуль) —
    пользователь всё равно получает ответ, владельцу алерт, легаси не зовётся."""
    alerts: list[tuple] = []

    import sreda.runtime.handlers as handlers_mod
    import sreda.runtime.planner_chat as pc

    def fake_alert(severity, title, body, **kw):
        alerts.append((severity, title, body))

    async def boom(**kw):
        raise ImportError("искусственный сбой на границе")

    monkeypatch.setattr(pc, "run_planner_chat_loop", boom)
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", fake_alert,
    )
    session, telegram, fake_llm = _run_turn(
        monkeypatch, tmp_path, f"g4_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL
        assert telegram.sent and telegram.sent[0]["text"] in BREAKDOWN_POOL
        assert not fake_llm._bound.calls
        assert alerts and "границе" in alerts[0][1]
    finally:
        session.close()


def test_called_tools_includes_post_invoke_failures(monkeypatch, tmp_path: Path):
    """Codex #120 R1 MAJOR: called_tools = физически вызванные (включая
    timeout/unknown_outcome), счётчики успехов — только чистые «ok»."""
    import sreda.runtime.planner_chat as pc
    import sreda.runtime.planner.orchestrator as orch
    import sreda.runtime.planner.executor as execu

    class _Step:
        def __init__(self, sid, tool, status):
            self.step_id, self.tool, self.status = sid, tool, status

    class _ExecLog:
        outcome = "partial_failure"
        steps = [_Step("s1", "add_task", "ok"),
                 _Step("s2", "add_shopping_items", "unknown_outcome"),
                 _Step("s3", "list_shopping", "skipped")]

    class _Plan:
        compose = None

    class _PlanResult:
        success = True
        final_attempt_no = 1
        error_summary = None
        execution_plan = object()
        plan = _Plan()

    async def fake_orch(ctx, **kw):
        return _PlanResult()

    async def fake_exec(*a, **kw):
        return _ExecLog()

    captured: dict = {}

    def fake_compose(*a, **kw):
        class _R:
            text = "ГОТОВО"
            fallback_used = None
        return _R()

    monkeypatch.setattr(orch, "run", fake_orch)
    monkeypatch.setattr(execu, "execute_plan", fake_exec)
    import importlib
    compose_mod = importlib.import_module("sreda.services.composer.compose")
    monkeypatch.setattr(compose_mod, "compose", fake_compose)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    async def run_and_capture(**kw):
        res = await _orig(**kw)
        captured["called"] = res.called_tools
        captured["counts"] = res.successful_tool_counts
        return res

    _orig = pc.run_planner_chat_loop
    monkeypatch.setattr(pc, "run_planner_chat_loop", run_and_capture)

    session, telegram, fake_llm = _run_turn(
        monkeypatch, tmp_path, f"g5_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert captured["called"] == {"add_task", "add_shopping_items"}, captured
        assert captured["counts"] == {"add_task": 1}, captured
        assert telegram.sent and telegram.sent[0]["text"] == "ГОТОВО"
    finally:
        session.close()


def test_aborted_partial_honest_template(monkeypatch, tmp_path: Path):
    """Codex #120 R1 CRITICAL: aborted_partial НЕ идёт в обычную сборку —
    пользователь получает честное «сделала часть…», владельцу алерт."""
    import sreda.runtime.planner_chat as pc
    import sreda.runtime.planner.orchestrator as orch
    import sreda.runtime.planner.executor as execu

    alerts: list[tuple] = []

    class _Step:
        def __init__(self, sid, tool, status):
            self.step_id, self.tool, self.status = sid, tool, status

    class _ExecLog:
        outcome = "aborted_partial"
        steps = [_Step("s1", "add_task", "ok"),
                 _Step("s2", "add_shopping_items", "unknown_outcome")]

    class _PlanResult:
        success = True
        final_attempt_no = 1
        error_summary = None
        execution_plan = object()
        plan = type("P", (), {"compose": None})()

    async def fake_orch(ctx, **kw):
        return _PlanResult()

    async def fake_exec(*a, **kw):
        return _ExecLog()

    def fail_compose(*a, **kw):
        raise AssertionError("compose() не должен вызываться для aborted_partial")

    monkeypatch.setattr(orch, "run", fake_orch)
    monkeypatch.setattr(execu, "execute_plan", fake_exec)
    import importlib
    compose_mod = importlib.import_module("sreda.services.composer.compose")
    monkeypatch.setattr(compose_mod, "compose", fail_compose)
    monkeypatch.setattr(pc, "send_admin_alert",
                        lambda sev, title, body, **kw: alerts.append((sev, title)))

    session, telegram, fake_llm = _run_turn(
        monkeypatch, tmp_path, f"g6_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent
        text = telegram.sent[0]["text"].lower()
        assert "сделала" in text or "не получилось" in text, text
        assert any("исполнение" in t for _, t in alerts), alerts
    finally:
        session.close()


def test_aborted_partial_without_clean_ok_is_uncertain(monkeypatch, tmp_path: Path):
    """Codex #120 R2 MAJOR: aborted_partial БЕЗ чистых успехов (только
    unknown_outcome) — никакого «Сделала что просила»; честная неуверенность."""
    import sreda.runtime.planner_chat as pc
    import sreda.runtime.planner.orchestrator as orch
    import sreda.runtime.planner.executor as execu

    class _Step:
        def __init__(self, sid, tool, status):
            self.step_id, self.tool, self.status = sid, tool, status

    class _ExecLog:
        outcome = "aborted_partial"
        steps = [_Step("s1", "add_shopping_items", "unknown_outcome")]

    class _PlanResult:
        success = True
        final_attempt_no = 1
        error_summary = None
        execution_plan = object()
        plan = type("P", (), {"compose": None})()

    async def fake_orch(ctx, **kw):
        return _PlanResult()

    async def fake_exec(*a, **kw):
        return _ExecLog()

    monkeypatch.setattr(orch, "run", fake_orch)
    monkeypatch.setattr(execu, "execute_plan", fake_exec)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    session, telegram, fake_llm = _run_turn(
        monkeypatch, tmp_path, f"g7_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent
        text = telegram.sent[0]["text"].lower()
        assert "не уверена" in text, text
        assert "сделала что просила" not in text, text
    finally:
        session.close()


def test_onboarding_turns_stay_on_legacy(monkeypatch, tmp_path: Path):
    """Codex #120 R2 MEDIUM: онбординг-ход гейченного тенанта — за легаси
    (продуктовое исключение; плановый контур онбордингу не обучен).
    Флаг онбординга выставляем принудительно через подмену preflight —
    тестовый скил обвязки не «хозяйка», и сам блок онбординга не выполняется."""
    import dataclasses

    import sreda.runtime.handlers as handlers_mod
    import sreda.runtime.planner_chat as pc

    _orig_preflight = handlers_mod._chat_preflight

    async def preflight_with_onboarding(session, action, context):
        pf = await _orig_preflight(session, action, context)
        if isinstance(pf, list):
            return pf
        return dataclasses.replace(pf, onboarding_follow_up_needed=True)

    async def planner_must_not_run(**kw):
        raise AssertionError("плановый контур не должен вызываться на онбординге")

    monkeypatch.setattr(handlers_mod, "_chat_preflight", preflight_with_onboarding)
    monkeypatch.setattr(pc, "run_planner_chat_loop", planner_must_not_run)

    session, telegram, fake_llm = _run_turn(
        monkeypatch, tmp_path, f"g8_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent and telegram.sent[0]["text"] == "ЛЕГАСИ ОТВЕТ"
        assert fake_llm._bound.calls, "онбординг-ход обязан идти через легаси"
    finally:
        session.close()
