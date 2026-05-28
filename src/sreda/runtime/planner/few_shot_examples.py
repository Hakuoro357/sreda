"""Curated few-shot examples for the planner system prompt.

Sub-A12 Phase B.1 (R3 — fixed after Codex review).

**Synthetic examples only** — no real PII from production traces. Real-
Boris fixtures live in gitignored ``private/planner_fixtures/boris_real/``
and are loaded only by snapshot tests (B.6), never embedded in the
prompt.

Each example is a triple (user_message, brief_context, plan_json). The
planner sees them in the cached prefix of the prompt so the model learns
plan shape, branching style, и когда переспрашивать.

**Contract invariants enforced for every example** (tests in
``test_planner_prompt_builder.py``):

* ``Plan.model_validate(plan)`` passes — covers clarity/clarity_reason/
  CLARIFICATION_TEMPLATE_IDS / MIXED_MODE_CLARIFICATION_TEMPLATE constraints.
* ``validate_plan(plan, registry=MIGRATED_TOOL_SPECS)`` returns no
  violations — tool names + arg shapes match real ToolSpec contracts.
* Every ``expected_outcomes[].match.status`` matches a Literal value in
  the producer tool's output_model.
* Every terminal branch has a ``compose`` block consistent with the
  status it triggers on (R2 fix: don't let empty/error fall through
  to a root compose that references unavailable fields like ``items``).
* ``REGISTRY.render(compose.template_id, compose.template_data)`` does
  NOT crash under StrictUndefined (with planner-promised data only;
  no executor-enrichment stubs).

R3 fix coverage:
- Every non-success branch has its OWN compose pointing at the right
  template (empty → shopping_added_empty etc.) so executor never
  falls through to a status-incompatible root compose.
- Dropped semantically-wrong examples (recipe chain via list indexing,
  parallel reads needing executor-enriched count, recall hardcoded
  "nothing found"). Kept 7 examples that work end-to-end.
- Statuses match real output_model Literals (added|empty|error, etc.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class FewShotExample:
    """One curated example. ``context_brief`` may be empty when the
    plan does not depend on memories/history/profile (most cases)."""

    user_message: str
    context_brief: str
    plan: dict


_EXAMPLES: list[FewShotExample] = [
    # ──────────────────────────────────────────────────────────────────
    # 1. add_shopping_items with explicit branch composes
    # statuses: added | empty | error
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="купи молоко и хлеб",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "явный запрос на добавление в покупки",
            },
            "clarity": "clear",
            "actions": {
                "s1": {
                    "tool": "add_shopping_items",
                    "args": {"items": [{"title": "молоко"}, {"title": "хлеб"}]},
                    "expected_outcomes": [
                        {
                            "match": {"status": "added"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "shopping_added_ok",
                                "template_data": {"items": ["молоко", "хлеб"]},
                            },
                        },
                        {
                            "match": {"status": "empty"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "shopping_added_empty",
                                "template_data": {"duplicates": ["молоко", "хлеб"]},
                            },
                        },
                        {
                            "match": {"status": "error"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "generic_tool_error",
                                "template_data": {"error_code": "${s1.error_code}"},
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
                "template_data": {"items": ["молоко", "хлеб"]},
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 2. Explicit-time reminder with branch composes
    # statuses: scheduled | skipped_past | error
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="напомни в 18:00 забрать ребёнка из садика",
        context_brief="now: 2026-05-28 14:30 Europe/Moscow",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "просьба напомнить с явным временем",
            },
            "clarity": "clear",
            "actions": {
                "s1": {
                    "tool": "schedule_reminder",
                    "args": {
                        "title": "забрать ребёнка из садика",
                        "trigger_iso": "2026-05-28T18:00:00+03:00",
                    },
                    "expected_outcomes": [
                        {
                            "match": {"status": "scheduled"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "reminder_set_ok",
                                "template_data": {
                                    "what": "забрать ребёнка из садика",
                                    "when_phrase": "сегодня в 18:00",
                                },
                            },
                        },
                        {
                            "match": {"status": "skipped_past"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "reminder_skipped_past",
                                "template_data": {
                                    "trigger_at_local": "${s1.trigger_at_iso}",
                                    "late_by_minutes": "${s1.late_by_minutes}",
                                },
                            },
                        },
                        {
                            "match": {"status": "error"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "generic_tool_error",
                                "template_data": {"error_code": "${s1.error_code}"},
                            },
                        },
                    ],
                    "intent_group": "default",
                    "depends_on": [],
                },
            },
            "compose": {
                "kind": "template",
                "template_id": "reminder_set_ok",
                "template_data": {
                    "what": "забрать ребёнка из садика",
                    "when_phrase": "сегодня в 18:00",
                },
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 3. Clarification — no time given for reminder
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="напомни купить хлеб",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "просьба напомнить без указания времени",
            },
            "clarity": "needs_clarification",
            "clarity_reason": "не указано время напоминания",
            "actions": {},
            "compose": {
                "kind": "template",
                "template_id": "ask_when_to_remind",
                "template_data": {"what": "купить хлеб"},
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 4. save_core_fact — durable user fact, mixed-mode acknowledge
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="запомни что у мужа аллергия на орехи",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "явная просьба запомнить стабильный факт",
            },
            "clarity": "needs_clarification",
            "clarity_reason": "запомнила: у мужа аллергия на орехи. Записать ещё что-то на эту тему?",
            "actions": {
                "s1": {
                    "tool": "save_core_fact",
                    "args": {"content": "у мужа аллергия на орехи"},
                    "expected_outcomes": [
                        {
                            "match": {"status": "saved_core"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "partial_with_clarification",
                                "template_data": {
                                    "done_summary": "запомнила: у мужа аллергия на орехи",
                                    "clarity_reason": "запомнила: у мужа аллергия на орехи. Записать ещё что-то на эту тему?",
                                },
                            },
                        },
                        {
                            "match": {"status": "error"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "generic_tool_error",
                                "template_data": {"error_code": "${s1.error_code}"},
                            },
                        },
                    ],
                    "intent_group": "default",
                    "depends_on": [],
                },
            },
            "compose": {
                "kind": "template",
                "template_id": "partial_with_clarification",
                "template_data": {
                    "done_summary": "запомнила: у мужа аллергия на орехи",
                },
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 5. Mixed write+ask: добавить + переспросить отдельную подцель
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="купи молоко. и напомни что-нибудь про машину",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "две независимые подцели — добавить + переспросить",
            },
            "clarity": "needs_clarification",
            "clarity_reason": "про машину непонятно что именно и когда напомнить",
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
                                "template_id": "partial_with_clarification",
                                "template_data": {
                                    "done_summary": "добавила молоко в покупки",
                                    "missing_fields": ["time", "items"],
                                    "clarity_reason": "про машину непонятно что именно и когда напомнить",
                                },
                            },
                        },
                        {
                            "match": {"status": "empty"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "partial_with_clarification",
                                "template_data": {
                                    "done_summary": "молоко уже было в покупках",
                                    "missing_fields": ["time", "items"],
                                    "clarity_reason": "про машину непонятно что именно и когда напомнить",
                                },
                            },
                        },
                        {
                            "match": {"status": "error"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "generic_tool_error",
                                "template_data": {"error_code": "${s1.error_code}"},
                            },
                        },
                    ],
                    "intent_group": "shopping",
                    "depends_on": [],
                },
            },
            "compose": {
                "kind": "template",
                "template_id": "partial_with_clarification",
                "template_data": {
                    "done_summary": "добавила молоко в покупки",
                    "missing_fields": ["time", "items"],
                },
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 6. Voice low-confidence → переспрос (no actions)
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="купи кошку",
        context_brief="voice_meta: is_voice=true, confidence=0.42",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "voice confidence < 0.7 + странный запрос — уточнить",
            },
            "clarity": "needs_clarification",
            "clarity_reason": "я расслышала «купи кошку» при низкой уверенности — это правда то что ты сказал?",
            "actions": {},
            "compose": {
                "kind": "template",
                "template_id": "ask_user_for_clarification",
                "template_data": {},
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 7. recipe_not_found-ish — single recipe ask without indexing
    # statuses: found | error
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="покажи мой рецепт борща с id rec_aaaa1111bbbb2222cccc3333",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "конкретный запрос рецепта по id",
            },
            "clarity": "clear",
            "actions": {
                "s1": {
                    "tool": "get_recipe",
                    "args": {"recipe_id": "rec_aaaa1111bbbb2222cccc3333"},
                    "expected_outcomes": [
                        {
                            "match": {"status": "found"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "recipe_show",
                                "template_data": {"recipe_text": "${s1.raw_text}"},
                            },
                        },
                        {
                            "match": {"status": "error"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "generic_tool_error",
                                "template_data": {"error_code": "${s1.error_code}"},
                            },
                        },
                    ],
                    "intent_group": "default",
                    "depends_on": [],
                },
            },
            "compose": {
                "kind": "template",
                "template_id": "recipe_show",
                "template_data": {"recipe_text": "${s1.raw_text}"},
            },
        },
    ),
]


def render_few_shot_block(*, indent: int = 2) -> str:
    """Render all examples as a markdown block for the system prompt.

    Output format:
    ```
    ### Пример 1: <user_message>
    Контекст: <brief>
    План:
    <pretty-printed JSON>
    ```

    Used by `prompt_builder.build_cached_prefix(...)`. Returns deterministic
    string so prompt cache hits across requests."""
    parts: list[str] = []
    for i, ex in enumerate(_EXAMPLES, start=1):
        parts.append(f"### Пример {i}: {ex.user_message}")
        if ex.context_brief:
            parts.append(f"Контекст: {ex.context_brief}")
        parts.append("План:")
        parts.append(
            json.dumps(ex.plan, ensure_ascii=False, indent=indent, sort_keys=False)
        )
        parts.append("")  # blank line between examples
    return "\n".join(parts).rstrip()


def example_count() -> int:
    """Public counter — used by snapshot tests."""
    return len(_EXAMPLES)


def all_examples() -> tuple[FewShotExample, ...]:
    """Public accessor for examples — used by validation tests so they
    can iterate without exposing the private list directly."""
    return tuple(_EXAMPLES)


__all__ = ["FewShotExample", "all_examples", "example_count", "render_few_shot_block"]
