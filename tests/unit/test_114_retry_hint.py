"""#114 — подсказка с allowed-статусами обязана доезжать до повтора (red-first).

Валидатор пишет «Allowed: [настоящие статусы]» прямо в текст нарушения
`branch_match_status_invented`, но оркестратор берёт первые 5 нарушений подряд
и режет склейку до 500 знаков — при многословном шуме впереди (ошибки типов,
ссылки) подсказка не доезжает до модели, и повтор слеп (ход #75 прогона r3).

Фикс: приоритизация нарушений с корректирующим словарём (Allowed / Available /
Bind) ПЕРЕД срезами. Семантика не меняется, авто-починки статусов нет.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sreda.runtime.planner.orchestrator import (
    PlannerContext,
    _prioritize_for_feedback,
    run,
)
from sreda.runtime.planner.validator import Violation


def test_priority_codes_go_first_stable():
    vs = [
        Violation(step_id="s1", tool="t", code="string_type", message="a"),
        Violation(step_id="s1", tool="t", code="string_type", message="b"),
        Violation(step_id="s2", tool="t", code="branch_match_status_invented",
                  message="... Allowed: ['ok']"),
        Violation(step_id="s1", tool="t", code="string_type", message="c"),
        Violation(step_id="s3", tool="t", code="composer_contract_invalid",
                  message="... Bind a literal"),
    ]
    out = _prioritize_for_feedback(vs)
    assert [v.code for v in out[:2]] == [
        "branch_match_status_invented", "composer_contract_invalid",
    ]
    # стабильность: шум сохраняет исходный порядок
    assert [v.message for v in out[2:]] == ["a", "b", "c"]


def _noisy_invented_status_plan() -> dict:
    """План: 6 шумных нарушений типов в аргументах + выдуманный статус ветки.
    Без приоритизации invented-нарушение выпадает из «первых 5» целиком."""
    return {
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                # 6 элементов с числовыми title → 6 string_type-нарушений
                "args": {"items": [{"title": i} for i in range(6)]},
                "expected_outcomes": [
                    {"match": {"status": "totally_invented"}, "next": None},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": "${s1.created}"},
        },
    }


def test_allowed_hint_survives_into_retry_prompt():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from sreda.runtime.planner.prompt_builder import NowMoment, ProfileSnapshot
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
    from sreda.services.composer.registry import REGISTRY
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

    prompts: list[str] = []

    class _Result:
        raw_text = json.dumps(_noisy_invented_status_plan(), ensure_ascii=False)
        latency_ms = 1
        provider = "fake"
        model = "fake"

    def fake_call(prompt: Any, *, attempt_no: int = 1, **_kw: Any):
        # PromptBundle | str — для проверки текст приводим к строке
        prompts.append(str(getattr(prompt, "user_message", prompt)))
        return _Result()

    ctx = PlannerContext(
        tenant_id="t1", run_id="r1", feature_key="housewife_assistant",
        user_message="добавь молоко", voice_meta=None,
        now=NowMoment(datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)),
        profile=ProfileSnapshot(name="Т"), memories=(),
        active_turn=None, closed_turns=(),
        available_tools=tuple(MIGRATED_TOOL_SPECS),
        composer_template_ids=tuple(REGISTRY.template_ids()),
        composer_llm_prompt_keys=tuple(LLM_PROMPT_REGISTRY.prompt_keys()),
        composer_registry_snapshot_hash=REGISTRY.snapshot_hash(),
        tool_registry_version="t114",
        few_shot_block="",
    )
    result = asyncio.run(run(ctx, session_factory=None, call_planner_fn=fake_call))
    assert not result.success and len(prompts) == 2
    retry_prompt = prompts[1]
    assert "branch_match_status_invented" in retry_prompt or "Allowed:" in retry_prompt
    # сама подсказка: настоящие статусы инструмента должны быть видны модели
    assert "Allowed:" in retry_prompt, retry_prompt[-700:]
    assert "added" in retry_prompt.split("Allowed:")[-1][:200]
