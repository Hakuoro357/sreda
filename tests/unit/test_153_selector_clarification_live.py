"""#153 Фаза 1 (ЯДРО) — generic-проводка уточнения .only в ЖИВОМ контуре.

Корневой диагноз: живой ``run_planner_chat_loop`` НЕ потреблял
``exec_log.abort_kind`` — селекторная неоднозначность (>1 или 0 совпадений по
``.only``) уходила generic-«поломкой» вместо уточнения. Машинерия уточнения
(``compose_for_execution`` → ``selector_ambiguity_clarification``) была мёртвым
кодом (нулевые живые вызовы с #120).

Тесты гоняют НАСТОЯЩИЙ ``run_planner_chat_loop`` (как
``test_planner_usage_recording.py``): подменяем только границы
(``orchestrator.run`` отдаёт план, ``executor.execute_plan`` отдаёт реальный
``ExecutionLog`` с ``abort_kind``), а ``compose_for_execution`` +
``selector_ambiguity_clarification`` — НАСТОЯЩИЕ. Голос (рот) — управляемая
заглушка ``DEFAULT_LLM_COMPOSER``.

Без wall-clock: «сейчас» в этих тестах не участвует (NowMoment внутри ctx плана
не влияет на abort-ветку), исход исполнения задаётся прямо.

Имена тестов — по чеклисту приёмки #153 (п.1, п.2, п.4, п.5).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sreda.runtime.planner.executor import ExecutionLog, StepResult
from sreda.runtime.planner.schemas import ComposerCall


# ---------------------------------------------------------------------------
# Харнес: прямой вызов run_planner_chat_loop с реальной проводкой clarification
# ---------------------------------------------------------------------------


def _pf() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-153",
        feature_key="housewife_assistant",
        user_text="удали напоминание про разминку",
        messages=[],
        tools_by_name={},
        _turn_msg_start_idx=0,
    )


def _plan_result(*, compose_template_id: str = "reminders_list_show") -> SimpleNamespace:
    """plan_result с реальным ComposerCall (роль plan.compose — её helper
    игнорирует для abort, но compose_for_execution принимает её на вход)."""
    plan = SimpleNamespace(
        compose=ComposerCall.model_validate(
            {
                "kind": "template",
                "template_id": compose_template_id,
                "template_data": {"items": "${s1.items}"},
            }
        ),
        turn_classification=SimpleNamespace(is_new_turn=True, reason="x"),
    )
    return SimpleNamespace(
        success=True,
        execution_plan=SimpleNamespace(),
        final_attempt_no=1,
        error_summary=None,
        plan=plan,
        raw_responses=(),
        prompt_tokens=0,
        completion_tokens=0,
        provider="inception-mercury2",
        model="mercury-2",
    )


def _selector_abort_log(
    *,
    outcome: str,
    options: list[str],
    count: int,
    extra_steps: tuple[StepResult, ...] = (),
) -> ExecutionLog:
    """Реальный ExecutionLog с abort_kind='selector_ambiguity'.

    s1 (list-шаг) status='arg_violation' (селекторный шаг исключается из
    ``called`` ровно как в проде); опциональные extra_steps моделируют
    уже-совершённые записи для aborted_partial."""
    steps = (
        *extra_steps,
        StepResult(step_id="s2", tool="cancel_reminder", status="arg_violation"),
    )
    return ExecutionLog(
        steps=steps,
        outcome=outcome,  # type: ignore[arg-type]
        abort_kind="selector_ambiguity",
        abort_details_public={"options": options, "count": count},
        abort_details_internal={
            "source_step_id": "s1",
            "target_tool": "cancel_reminder",
            "arg_path": "reminder_id",
        },
    )


async def _drive(
    monkeypatch,
    *,
    exec_log: ExecutionLog,
    voice_text: str | None,
    voice_raises: bool = False,
    plan_result: SimpleNamespace | None = None,
):
    """Гоняет run_planner_chat_loop с подменёнными границами; возвращает
    (reply_text, voice_calls) где voice_calls — список kwargs вызовов рта."""
    from sreda.runtime import planner_chat
    from sreda.runtime.planner import executor as _executor_mod
    from sreda.runtime.planner import orchestrator
    from sreda.services.composer import llm_composer as _voice_mod
    from sreda.services.composer.llm_composer import LLMComposerResult

    monkeypatch.setattr(planner_chat, "send_admin_alert", lambda *a, **k: None)
    monkeypatch.setattr(
        planner_chat, "_persona_preset_or_none", lambda *a, **k: None
    )
    # usage-запись не нужна (session=None её и так пропускает)
    pr = plan_result or _plan_result()

    async def fake_run(ctx, **kw):
        return pr

    async def fake_exec(execution_plan, tools, registry):
        return exec_log

    monkeypatch.setattr(orchestrator, "run", fake_run)
    monkeypatch.setattr(_executor_mod, "execute_plan", fake_exec)

    voice_calls: list[dict] = []

    def fake_voice(**kw):
        voice_calls.append(kw)
        if voice_raises:
            raise RuntimeError("искусственный сбой рта")
        return LLMComposerResult(
            text=voice_text or "",
            provider="openrouter-gemini-2.5-flash-lite",
            model="google/gemini-2.5-flash-lite",
            latency_ms=10,
            prompt_tokens=5,
            completion_tokens=5,
        )

    monkeypatch.setattr(_voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)

    res = await planner_chat.run_planner_chat_loop(
        session=None,
        action=SimpleNamespace(tenant_id="t-153"),
        pf=pr and _pf() or _pf(),
        context={},
    )
    return res.final_ai.content, voice_calls


def _breakdown_pool() -> set[str]:
    from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL

    return set(BREAKDOWN_POOL)


def _ordered_subsequence(haystack: str, needles: list[str]) -> bool:
    """True если needles встречаются в haystack в этом ПОРЯДКЕ (как подпослед.)."""
    pos = 0
    for n in needles:
        idx = haystack.find(n, pos)
        if idx < 0:
            return False
        pos = idx + len(n)
    return True


# ---------------------------------------------------------------------------
# п.1 — аборт (>1 совпадение) → уточнение ЧЕРЕЗ РОТ, порядок+count сохранены
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aborted_selector_ambiguity_voiced_clarification_ordered(monkeypatch):
    """#153 п.1: list_reminders→cancel(.only) с ≥2 совпадениями → уточнение
    (варианты в ПОРЯДКЕ + count), прошло РОТ (рот вызван, текст ≠ сырой шаблон),
    НЕ _fallback_error/breakdown."""
    options = ["Разминка утром (08:00)", "Разминка вечером (19:00)", "Разминка днём (13:00)"]
    log = _selector_abort_log(outcome="aborted", options=options, count=3)

    voiced = (
        "Нашла 3 напоминания про разминку — какое отменить: "
        "Разминка утром (08:00), Разминка вечером (19:00), Разминка днём (13:00)?"
    )
    text, voice_calls = await _drive(monkeypatch, exec_log=log, voice_text=voiced)

    # прошло рот (важно: called пуст — селекторный шаг = arg_violation)
    assert voice_calls, "рот обязан был отработать на уточнении (минуя гейт `and called`)"
    assert voice_calls[0]["llm_prompt_key"] == "humanize_result"
    # это именно живой голос, не сырой детерминированный шаблон
    assert text == voiced
    assert text not in _breakdown_pool()
    # count + варианты в ПОРЯДКЕ сохранены
    assert "3" in text
    assert _ordered_subsequence(text, options), text


@pytest.mark.asyncio
async def test_aborted_partial_selector_preserves_done_summary(monkeypatch):
    """#153 п.5: aborted_partial + selector_ambiguity → уточнение сохраняет
    «часть уже сделала» (done_summary) ПОСЛЕ рта + варианты в порядке."""
    options = ["Разминка утром (08:00)", "Разминка вечером (19:00)"]
    prior = (StepResult(step_id="s0", tool="add_shopping_items", status="ok"),)
    log = _selector_abort_log(
        outcome="aborted_partial", options=options, count=2, extra_steps=prior
    )

    voiced = (
        "Часть действий уже выполнила. А вот напоминаний про разминку — 2, "
        "уточни какое: Разминка утром (08:00), Разминка вечером (19:00)?"
    )
    text, voice_calls = await _drive(monkeypatch, exec_log=log, voice_text=voiced)

    assert voice_calls, "рот обязан был отработать"
    assert text == voiced
    assert text not in _breakdown_pool()
    # honesty-инвариант: «часть» сохранена
    assert "часть" in text.lower()
    assert "2" in text
    assert _ordered_subsequence(text, options), text


# ---------------------------------------------------------------------------
# Guard сохранности: рот переставил/потерял варианты → детерм. фолбэк
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_drops_options_falls_back_to_deterministic(monkeypatch):
    """Order-guard: рот «потерял» один вариант → НЕ теряем варианты, отдаём
    детерминированный шаблон (с count + всеми вариантами)."""
    options = ["Разминка утром (08:00)", "Разминка вечером (19:00)", "Разминка днём (13:00)"]
    log = _selector_abort_log(outcome="aborted", options=options, count=3)

    # рот выкинул третий вариант
    voiced = "Какое из двух: Разминка утром (08:00), Разминка вечером (19:00)?"
    text, voice_calls = await _drive(monkeypatch, exec_log=log, voice_text=voiced)

    assert voice_calls, "рот вызывался"
    # фолбэк на детерминированный шаблон: все варианты + count сохранены
    assert text != voiced
    assert text not in _breakdown_pool()
    assert "3" in text
    assert _ordered_subsequence(text, options), text


@pytest.mark.asyncio
async def test_voice_reorders_options_falls_back_to_deterministic(monkeypatch):
    """Order-guard: рот ПЕРЕСТАВИЛ варианты → детерминированный фолбэк
    (исходный порядок сохранён)."""
    options = ["Разминка утром (08:00)", "Разминка вечером (19:00)"]
    log = _selector_abort_log(outcome="aborted", options=options, count=2)

    # порядок инвертирован
    voiced = "Уточни: Разминка вечером (19:00) или Разминка утром (08:00)?"
    text, voice_calls = await _drive(monkeypatch, exec_log=log, voice_text=voiced)

    assert voice_calls
    assert text != voiced
    assert _ordered_subsequence(text, options), text


@pytest.mark.asyncio
async def test_voice_blank_falls_back_to_deterministic(monkeypatch):
    """Пустой ответ рта → детерминированный шаблон (варианты не теряются)."""
    options = ["Разминка утром (08:00)", "Разминка вечером (19:00)"]
    log = _selector_abort_log(outcome="aborted", options=options, count=2)

    text, voice_calls = await _drive(monkeypatch, exec_log=log, voice_text="")

    assert voice_calls
    assert text not in _breakdown_pool()
    assert "2" in text
    assert _ordered_subsequence(text, options), text


@pytest.mark.asyncio
async def test_voice_exception_falls_back_to_deterministic(monkeypatch):
    """Исключение в рту → детерминированный шаблон, варианты сохранены."""
    options = ["Разминка утром (08:00)", "Разминка вечером (19:00)"]
    log = _selector_abort_log(outcome="aborted", options=options, count=2)

    text, voice_calls = await _drive(
        monkeypatch, exec_log=log, voice_text=None, voice_raises=True
    )

    assert voice_calls
    assert text not in _breakdown_pool()
    assert "2" in text
    assert _ordered_subsequence(text, options), text


# ---------------------------------------------------------------------------
# Регресс: НЕ-селекторные аборты → прежний breakdown (НЕ трогаем)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_selector_aborted_still_breakdown(monkeypatch):
    """Регресс (#153 п.2): аборт БЕЗ abort_kind (например, arg_violation от
    другого шага) → прежний breakdown, НЕ уточнение, рот не зовётся для clar."""
    log = ExecutionLog(
        steps=(
            StepResult(step_id="s1", tool="add_task", status="arg_violation"),
        ),
        outcome="aborted",
        abort_kind=None,
        abort_details_public=None,
    )
    text, voice_calls = await _drive(monkeypatch, exec_log=log, voice_text="нечто")

    # breakdown из пула, рот для clarification НЕ звался
    assert text in _breakdown_pool(), text
    assert not voice_calls, "не-селекторный аборт не должен звать рот"


@pytest.mark.asyncio
async def test_non_selector_aborted_partial_still_partial_path(monkeypatch):
    """Регресс: aborted_partial БЕЗ abort_kind → прежняя partial-ветка
    (честная неуверенность / «сделала часть»), НЕ уточнение."""
    log = ExecutionLog(
        steps=(
            StepResult(step_id="s1", tool="add_task", status="ok"),
            StepResult(step_id="s2", tool="add_shopping_items", status="unknown_outcome"),
        ),
        outcome="aborted_partial",
        abort_kind=None,
        abort_details_public=None,
    )
    text, voice_calls = await _drive(monkeypatch, exec_log=log, voice_text="нечто")

    low = text.lower()
    # прежняя partial-ветка (есть чистый ok → partial_with_compose_error или
    # его breakdown-фолбэк); НЕ уточнение
    assert "уточни" not in low, text
    assert not voice_calls, "не-селекторный partial не должен звать рот для clar"


# ---------------------------------------------------------------------------
# п.4 — 0-match идёт НЕ через selector (empty-ветка продюсера), не через clar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_match_empty_branch_not_clarification(monkeypatch):
    """#153 п.4: 0 совпадений (list_reminders status=empty → empty-ветка
    продюсера ДО .only) → нормальный показ «не нашла», completed-исход. Это НЕ
    селекторный аборт (abort_kind=None) и НЕ breakdown."""
    from sreda.services.composer.llm_composer import LLMComposerResult
    from sreda.runtime import planner_chat
    from sreda.runtime.planner import executor as _executor_mod
    from sreda.runtime.planner import orchestrator
    from sreda.services.composer import llm_composer as _voice_mod

    # план завершён empty-веткой: терминальный compose шага s1 = reminders_list_empty
    empty_compose = ComposerCall.model_validate(
        {"kind": "template", "template_id": "reminders_list_empty", "template_data": {}}
    )
    log = ExecutionLog(
        steps=(
            StepResult(
                step_id="s1",
                tool="list_reminders",
                status="ok",
                parsed_output={"status": "empty"},
                selected_compose=empty_compose,
            ),
        ),
        outcome="completed",
        abort_kind=None,
        abort_details_public=None,
    )

    monkeypatch.setattr(planner_chat, "send_admin_alert", lambda *a, **k: None)
    monkeypatch.setattr(planner_chat, "_persona_preset_or_none", lambda *a, **k: None)

    pr = _plan_result()

    async def fake_run(ctx, **kw):
        return pr

    async def fake_exec(execution_plan, tools, registry):
        return log

    monkeypatch.setattr(orchestrator, "run", fake_run)
    monkeypatch.setattr(_executor_mod, "execute_plan", fake_exec)
    # рот прихорашивает completed-ход — отдаём предсказуемый текст
    monkeypatch.setattr(
        _voice_mod,
        "DEFAULT_LLM_COMPOSER",
        lambda **kw: LLMComposerResult(
            text="Активных напоминаний про разминку не нашла.",
            provider="p", model="m", latency_ms=1,
        ),
    )

    res = await planner_chat.run_planner_chat_loop(
        session=None, action=SimpleNamespace(tenant_id="t-153"),
        pf=_pf(), context={},
    )
    text = res.final_ai.content
    assert "уточни" not in text.lower(), text
    assert text not in _breakdown_pool(), text
    assert "не нашла" in text.lower() or "не " in text.lower()
