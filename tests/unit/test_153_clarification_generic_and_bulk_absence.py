"""#153 — общий фикс уточнения (чек-листы/задачи) + отсутствие bulk-инструмента.

Фаза 1 чинит уточнение .only для ЛЮБОГО типа (напоминания, чек-листы, задачи) —
проводка generic, не привязана к reminders. Эти тесты фиксируют:
- чек-лист .only (≥2) и задача .only (≥2) → уточнение через ту же проводку;
- bulk-инструмента (cancel_reminders / delete_reminders / *_bulk) НЕТ в реестре/
  промпте (descope #154 подтверждён — чеклист #153 п.6).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sreda.runtime.planner.executor import ExecutionLog, StepResult
from sreda.runtime.planner.schemas import ComposerCall


def _pf() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-153g", feature_key="housewife_assistant",
        user_text="удали разминку", messages=[], tools_by_name={},
        _turn_msg_start_idx=0,
    )


def _plan_result() -> SimpleNamespace:
    plan = SimpleNamespace(
        compose=ComposerCall.model_validate(
            {"kind": "template", "template_id": "checklist_items_show",
             "template_data": {"items": "${s1.items}"}}
        ),
        turn_classification=SimpleNamespace(is_new_turn=True, reason="x"),
    )
    return SimpleNamespace(
        success=True, execution_plan=SimpleNamespace(), final_attempt_no=1,
        error_summary=None, plan=plan, raw_responses=(),
        prompt_tokens=0, completion_tokens=0,
        provider="inception-mercury2", model="mercury-2",
    )


async def _drive_selector_abort(monkeypatch, *, options, count, voice_text):
    from sreda.runtime import planner_chat
    from sreda.runtime.planner import executor as _executor_mod
    from sreda.runtime.planner import orchestrator
    from sreda.services.composer import llm_composer as _voice_mod
    from sreda.services.composer.llm_composer import LLMComposerResult

    monkeypatch.setattr(planner_chat, "send_admin_alert", lambda *a, **k: None)
    monkeypatch.setattr(planner_chat, "_persona_preset_or_none", lambda *a, **k: None)

    log = ExecutionLog(
        steps=(
            StepResult(step_id="s2", tool="mark_checklist_item_done", status="arg_violation"),
        ),
        outcome="aborted",
        abort_kind="selector_ambiguity",
        abort_details_public={"options": options, "count": count},
        abort_details_internal={"source_step_id": "s1", "target_tool": "x", "arg_path": "y"},
    )
    pr = _plan_result()

    async def fake_run(ctx, **kw):
        return pr

    async def fake_exec(execution_plan, tools, registry):
        return log

    monkeypatch.setattr(orchestrator, "run", fake_run)
    monkeypatch.setattr(_executor_mod, "execute_plan", fake_exec)
    monkeypatch.setattr(
        _voice_mod, "DEFAULT_LLM_COMPOSER",
        lambda **kw: LLMComposerResult(
            text=voice_text, provider="p", model="m", latency_ms=1,
        ),
    )
    res = await planner_chat.run_planner_chat_loop(
        session=None, action=SimpleNamespace(tenant_id="t-153g"),
        pf=_pf(), context={},
    )
    return res.final_ai.content


@pytest.mark.asyncio
async def test_checklist_only_ambiguity_clarifies(monkeypatch):
    """Регресс/общий фикс: чек-лист .only (≥2) → уточнение (тот же путь)."""
    options = ["Лопата (Дача)", "Лопата снегоуборочная (Гараж)"]
    voiced = "Нашла 2 пункта: Лопата (Дача), Лопата снегоуборочная (Гараж) — какой?"
    text = await _drive_selector_abort(
        monkeypatch, options=options, count=2, voice_text=voiced
    )
    from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL
    assert text not in set(BREAKDOWN_POOL), text
    assert "Лопата" in text


@pytest.mark.asyncio
async def test_task_only_ambiguity_clarifies(monkeypatch):
    """Общий фикс: задача .only (≥2) → уточнение (тот же путь)."""
    options = ["Купить подарок", "Купить продукты"]
    voiced = "Какую из 2 задач: Купить подарок, Купить продукты?"
    text = await _drive_selector_abort(
        monkeypatch, options=options, count=2, voice_text=voiced
    )
    from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL
    assert text not in set(BREAKDOWN_POOL), text
    assert "2" in text


# ---------------------------------------------------------------------------
# Bulk-absence (#153 п.6 / descope #154)
# ---------------------------------------------------------------------------


def test_no_bulk_reminder_tool_in_registry():
    """В реестре инструментов НЕТ bulk-отмены напоминаний (descope в #154)."""
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

    names = {s.name for s in MIGRATED_TOOL_SPECS}
    banned = {"cancel_reminders", "delete_reminders", "cancel_all_reminders",
              "cancel_reminder_bulk", "bulk_cancel_reminders"}
    assert not (names & banned), f"bulk-инструмент просочился: {names & banned}"
    # одиночный cancel_reminder — на месте
    assert "cancel_reminder" in names
