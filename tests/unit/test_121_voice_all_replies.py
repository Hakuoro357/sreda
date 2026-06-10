"""#121 — рот для ВСЕХ ответов планового контура.

Правило владельца (скриншоты 2026-06-10, «вообще все ответы!!»): шаблонный
рендер — сырьё для humanize_result, не финал. Чеклист #121:
- шаблонный путь с исполненными шагами → прихорашивание вызвано, пользователю
  уходит живой текст;
- llm-путь (голос уже звучал в сборке) → второго вызова нет;
- сбой голоса → детерминированный текст (страховка) + алерт владельцу;
- ход без исполненных шагов → без прихорашивания.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from test_conversation_chat import (
    ConstantEmbeddingClient,  # noqa: F401 — реэкспорт обвязки
    FakeLLM,
    FakeTelegram,
)
from test_120_planner_gate import _run_turn

import sreda.runtime.planner_chat as pc
import sreda.runtime.planner.executor as execu
import sreda.runtime.planner.orchestrator as orch
from sreda.services.composer import llm_composer as voice_mod


class _Step:
    def __init__(self, sid, tool, status):
        self.step_id, self.tool, self.status = sid, tool, status


def _exec_log(outcome="completed", steps=None):
    class _E:
        pass
    e = _E()
    e.outcome = outcome
    e.steps = steps if steps is not None else [_Step("s1", "list_shopping", "ok")]
    return e


def _plan_result(compose_obj=None):
    class _P:
        pass
    p = _P()
    p.success = True
    p.final_attempt_no = 1
    p.error_summary = None
    p.execution_plan = object()
    p.plan = type("Plan", (), {"compose": compose_obj})()
    return p


def _wire(monkeypatch, *, exec_log, compose_result_text="В списке покупок: • молоко",
          compose_uses_voice=False):
    async def fake_orch(ctx, **kw):
        return _plan_result()

    async def fake_exec(*a, **kw):
        return exec_log

    import importlib
    compose_mod = importlib.import_module("sreda.services.composer.compose")

    def fake_compose(call, log, *, llm_composer=None, ctx=None, **kw):
        if compose_uses_voice and llm_composer is not None:
            llm_composer(
                llm_prompt_key="humanize_result",
                template_data={"intent": "x",
                               "actions": [{"user_visible_summary": "y",
                                            "status": "ok"}]},
                execution_log=log, ctx=ctx,
            )

        class _R:
            text = compose_result_text
            fallback_used = None
        return _R()

    monkeypatch.setattr(orch, "run", fake_orch)
    monkeypatch.setattr(execu, "execute_plan", fake_exec)
    monkeypatch.setattr(compose_mod, "compose", fake_compose)


def test_template_path_goes_through_voice(monkeypatch, tmp_path: Path):
    voice_calls: list[dict] = []

    def fake_voice(**kw):
        voice_calls.append(kw)

        class _V:
            text = "🛒 Вот что у нас в списке: молоко! Что-то ещё нужно?"
        return _V()

    _wire(monkeypatch, exec_log=_exec_log())
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"v1_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert voice_calls, "шаблонный путь обязан пройти через рот"
        summary = voice_calls[0]["template_data"]["actions"][0]["user_visible_summary"]
        assert "молоко" in summary  # детерминированный текст — сырьё голоса
        assert telegram.sent and "Вот что у нас" in telegram.sent[0]["text"]
    finally:
        session.close()


def test_llm_path_not_voiced_twice(monkeypatch, tmp_path: Path):
    voice_calls: list[dict] = []

    def fake_voice(**kw):
        voice_calls.append(kw)

        class _V:
            text = "живой ответ из сборки"
        return _V()

    _wire(monkeypatch, exec_log=_exec_log(), compose_uses_voice=True,
          compose_result_text="живой ответ из сборки")
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"v2_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        # ровно один вызов — внутри сборки; прихорашиватель не дублирует
        assert len(voice_calls) == 1, voice_calls
        assert telegram.sent and telegram.sent[0]["text"] == "живой ответ из сборки"
    finally:
        session.close()


def test_voice_failure_falls_back_to_template_with_alert(monkeypatch, tmp_path: Path):
    alerts: list[tuple] = []

    def fake_voice(**kw):
        raise RuntimeError("голос упал")

    _wire(monkeypatch, exec_log=_exec_log())
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert",
                        lambda sev, title, body, **kw: alerts.append((sev, title)))

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"v3_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent
        assert "В списке покупок" in telegram.sent[0]["text"]  # страховка
        assert any("прихорашивание" in t for _, t in alerts), alerts
    finally:
        session.close()


def test_no_steps_turn_skips_voice(monkeypatch, tmp_path: Path):
    voice_calls: list[dict] = []

    def fake_voice(**kw):
        voice_calls.append(kw)

        class _V:
            text = "не должен звучать"
        return _V()

    _wire(monkeypatch, exec_log=_exec_log(steps=[]),
          compose_result_text="Уточни, что именно показать?")
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"v4_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert not voice_calls, "ход без шагов не прихорашивается"
        assert telegram.sent and "Уточни" in telegram.sent[0]["text"]
    finally:
        session.close()


def test_compose_fallback_still_voiced(monkeypatch, tmp_path: Path):
    """Codex #121 R1 MAJOR (оба): НЕголосовая деградация сборки (шаблон/реестр)
    — её текст тоже сырьё для рта, плюс алерт о деградации."""
    import importlib
    alerts: list[tuple] = []
    voice_calls: list[dict] = []

    async def fake_orch(ctx, **kw):
        return _plan_result()

    async def fake_exec(*a, **kw):
        return _exec_log()

    compose_mod = importlib.import_module("sreda.services.composer.compose")

    def fake_compose(call, log, *, llm_composer=None, ctx=None, **kw):
        class _R:
            text = "Сделала что просила: list_shopping."
            fallback_used = "compose_error"  # НЕ llm_composer-сбой
        return _R()

    def fake_voice(**kw):
        voice_calls.append(kw)

        class _V:
            text = "🛒 Вот список! Что-то ещё?"
            latency_ms = 42
        return _V()

    monkeypatch.setattr(orch, "run", fake_orch)
    monkeypatch.setattr(execu, "execute_plan", fake_exec)
    monkeypatch.setattr(compose_mod, "compose", fake_compose)
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert",
                        lambda sev, title, body, **kw: alerts.append((sev, title)))

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"v5_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert voice_calls, "после неголосовой деградации рот обязателен"
        assert telegram.sent and "Вот список" in telegram.sent[0]["text"]
        assert any("сборка" in t for _, t in alerts), alerts
    finally:
        session.close()


def test_voice_broken_in_compose_not_retried(monkeypatch, tmp_path: Path):
    """Сломался сам голос внутри сборки (llm_composer_error) — повторный вызов
    того же голоса бессмыслен; уходит детерминированная страховка."""
    import importlib
    voice_calls: list[dict] = []

    async def fake_orch(ctx, **kw):
        return _plan_result()

    async def fake_exec(*a, **kw):
        return _exec_log()

    compose_mod = importlib.import_module("sreda.services.composer.compose")

    def fake_compose(call, log, *, llm_composer=None, ctx=None, **kw):
        class _R:
            # продовая форма (compose.py): сбой голоса живёт в error_code,
            # fallback_used при этом generic_error (Codex R2 medium)
            text = "Ой, не получилось красиво — но всё сделала."
            fallback_used = "generic_error"
            error_code = "llm_composer_error:RuntimeError"
        return _R()

    def fake_voice(**kw):
        voice_calls.append(kw)

        class _V:
            text = "не должен звучать"
        return _V()

    monkeypatch.setattr(orch, "run", fake_orch)
    monkeypatch.setattr(execu, "execute_plan", fake_exec)
    monkeypatch.setattr(compose_mod, "compose", fake_compose)
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"v6_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert not voice_calls, "сломанный голос не вызываем повторно"
        assert telegram.sent and "всё сделала" in telegram.sent[0]["text"]
    finally:
        session.close()


def test_blank_voice_output_alerts(monkeypatch, tmp_path: Path):
    """Codex #121 R1 (оба): пустой выход рта = сбой — алерт + страховка."""
    alerts: list[tuple] = []

    def fake_voice(**kw):
        class _V:
            text = "   "
        return _V()

    _wire(monkeypatch, exec_log=_exec_log())
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert",
                        lambda sev, title, body, **kw: alerts.append((title, body)))

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"v7_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent and "В списке покупок" in telegram.sent[0]["text"]
        assert any("blank_output" in b for _, b in alerts), alerts
    finally:
        session.close()


def test_arg_violation_only_turn_skips_voice(monkeypatch, tmp_path: Path):
    """Codex #121 R1 MAJOR (high): только до-вызовные arg_violation — реальных
    шагов нет, рот не зовём (предикат = called)."""
    voice_calls: list[dict] = []

    def fake_voice(**kw):
        voice_calls.append(kw)

        class _V:
            text = "не должен звучать"
        return _V()

    _wire(monkeypatch,
          exec_log=_exec_log(outcome="failed",
                             steps=[_Step("s1", "add_task", "arg_violation")]),
          compose_result_text="Не получилось обработать запрос.")
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"v8_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert not voice_calls, voice_calls
        assert telegram.sent
    finally:
        session.close()
