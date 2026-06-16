"""F-1 (#151): run_planner_chat_loop пишет usage планировщика + рта в skill_ai_executions.

Корень: до F-1 ход планового контура НЕ оставлял строк usage (orchestrator с
session_factory=None), поэтому траты Mercury/рта невидимы для админки денег.
Этот тест фиксирует контракт: успешный ход → ровно 2 строки usage с реальным
provider_key и токенами (планировщик + рот).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant
from sreda.db.models.skill_platform import SkillAIExecution


@pytest.fixture()
def session(tmp_path):
    # Файловый SQLite (не :memory:) — _record_usage_safe пишет в ОТДЕЛЬНОЙ
    # session на том же engine (новое соединение); файл общий, поэтому строки
    # видны тестовой session. У :memory: каждое соединение = своя БД.
    engine = create_engine(f"sqlite:///{tmp_path / 'f1.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="t-rec", name="T"))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _pf() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-rec", feature_key="housewife_assistant",
        user_text="что в меню", messages=[], tools_by_name={},
        _turn_msg_start_idx=0,
    )


@pytest.mark.asyncio
async def test_planner_and_rot_usage_recorded(session, monkeypatch) -> None:
    from sreda.runtime import planner_chat
    from sreda.runtime.planner import executor as _executor_mod
    import importlib

    from sreda.runtime.planner import orchestrator
    from sreda.services.composer import llm_composer as _voice_mod
    from sreda.services.composer.llm_composer import LLMComposerResult

    # composer/__init__ реэкспортирует функцию `compose`, затеняя сабмодуль
    # того же имени → берём настоящий модуль через import_module (sys.modules).
    _compose_mod = importlib.import_module("sreda.services.composer.compose")

    monkeypatch.setattr(planner_chat, "send_admin_alert", lambda *a, **k: None)
    monkeypatch.setattr(planner_chat, "_persona_preset_or_none",
                        lambda *a, **k: None)

    plan = SimpleNamespace(
        compose=SimpleNamespace(template_id="t", llm_prompt_key="k"),
        turn_classification=SimpleNamespace(is_new_turn=True, reason="x"),
    )

    async def fake_run(ctx, **kw):
        return SimpleNamespace(
            success=True, execution_plan=SimpleNamespace(), final_attempt_no=1,
            error_summary=None, plan=plan, raw_responses=(),
            prompt_tokens=1500, completion_tokens=120,
            provider="inception-mercury2", model="mercury-2",
        )

    monkeypatch.setattr(orchestrator, "run", fake_run)

    async def fake_exec(execution_plan, tools, registry):
        return SimpleNamespace(
            outcome="completed",
            steps=(SimpleNamespace(step_id="s1", tool="list_menu", status="ok"),),
        )

    monkeypatch.setattr(_executor_mod, "execute_plan", fake_exec)

    # compose() ДОЛЖЕН позвать llm_composer (_tracking_voice) — так рот-usage
    # захватывается обёрткой.
    def fake_compose(spec, exec_log, *, llm_composer, ctx, **kw):
        r = llm_composer(
            llm_prompt_key="k", template_data={}, execution_log=exec_log, ctx=ctx,
        )
        return SimpleNamespace(
            text=r.text, fallback_used=None, error_code=None,
            effective_template_id="t",
        )

    monkeypatch.setattr(_compose_mod, "compose", fake_compose)
    monkeypatch.setattr(
        _voice_mod, "DEFAULT_LLM_COMPOSER",
        lambda **kw: LLMComposerResult(
            text="Готово!", provider="openrouter-gemini-2.5-flash-lite",
            model="google/gemini-2.5-flash-lite", latency_ms=10,
            prompt_tokens=800, completion_tokens=60,
        ),
    )

    await planner_chat.run_planner_chat_loop(
        session=session, action=SimpleNamespace(tenant_id="t-rec"),
        pf=_pf(), context={},
    )

    rows = session.query(SkillAIExecution).filter_by(tenant_id="t-rec").all()
    # РОВНО 2 строки — ловит двойную запись/дубли (Codex R1 MINOR).
    assert len(rows) == 2, f"ожидаем ровно 2 строки usage, получили {len(rows)}"
    by_provider = {r.provider_key: r for r in rows}

    assert "inception-mercury2" in by_provider, "нет строки usage планировщика"
    planner_row = by_provider["inception-mercury2"]
    assert planner_row.prompt_tokens == 1500
    assert planner_row.completion_tokens == 120
    assert planner_row.model == "mercury-2"
    assert planner_row.task_type == "planner.plan"
    # Наблюдательная строка: квоту НЕ потребляет (Codex R2 high MAJOR).
    assert planner_row.credits_consumed == 0

    assert "openrouter-gemini-2.5-flash-lite" in by_provider, "нет строки usage рта"
    rot_row = by_provider["openrouter-gemini-2.5-flash-lite"]
    assert rot_row.prompt_tokens == 800
    assert rot_row.completion_tokens == 60
    assert rot_row.task_type == "composer.voice"
    assert rot_row.credits_consumed == 0
