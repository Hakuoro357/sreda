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
from typing import Iterable


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
                        # added — без своего compose: совпадает с root
                        # shopping_added_ok, падает туда (экономия префикса #128).
                        {"match": {"status": "added"}, "next": None},
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
    # ──────────────────────────────────────────────────────────────────
    # 8. Pure greeting → reply_only + smalltalk LLM
    # rot-enablement Phase 2 (issue #88): planner classifies pure
    # greetings/chit-chat as reply_only with kind='llm', key='smalltalk'.
    # actions MUST be empty. template_data MUST have user_message.
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="привет",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "чистое приветствие без задачи",
            },
            "clarity": "reply_only",
            "clarity_reason": None,
            "actions": {},
            "compose": {
                "kind": "llm",
                "llm_prompt_key": "smalltalk",
                "template_data": {"user_message": "привет"},
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 9. Identity question → reply_only + identity_playful template
    # rot-enablement Phase 2 (issue #88): identity questions get the
    # deterministic template (never leaks real model name, cheaper,
    # always consistent).
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="кто ты?",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "вопрос об идентичности ассистента",
            },
            "clarity": "reply_only",
            "clarity_reason": None,
            "actions": {},
            "compose": {
                "kind": "template",
                "template_id": "identity_playful",
                "template_data": {},
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 10. Mixed greeting+action → clear with actions (NOT reply_only)
    # Guard example: a request that looks conversational but contains a
    # real task MUST route through clarity='clear' with tool calls.
    # reply_only is NEVER correct when there is a task intent.
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="привет! добавь молоко в список",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "есть конкретная задача — добавить в покупки",
            },
            "clarity": "clear",
            "clarity_reason": None,
            "actions": {
                "s1": {
                    "tool": "add_shopping_items",
                    "args": {"items": [{"title": "молоко"}]},
                    "expected_outcomes": [
                        # added — без своего compose: падает в root
                        # shopping_added_ok (идентичен). Экономия префикса #128.
                        {"match": {"status": "added"}, "next": None},
                        {
                            "match": {"status": "empty"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "shopping_added_empty",
                                "template_data": {"duplicates": ["молоко"]},
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
                "template_data": {"items": ["молоко"]},
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 11 (was 12). Structured clarification — missing_fields with enum codes
    # Shows the closed-enum contract: missing_fields must contain only
    # recognised codes from the CLARIFICATION_FIELDS set.
    # Planner heard "напомни про встречу" — knows the subject but not the
    # time, and the user said "в пятницу" (ambiguous — no clock time).
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="напомни про встречу в пятницу",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "просьба напомнить — дата частично указана, время не названо",
            },
            "clarity": "needs_clarification",
            "clarity_reason": "не указано точное время напоминания",
            "actions": {},
            "compose": {
                "kind": "template",
                "template_id": "ask_user_for_clarification",
                "template_data": {
                    "missing_fields": ["time"],
                    "clarity_reason": "не указано точное время напоминания",
                },
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 12. web_search → humanize_result (Family A + C fix; #110 new {step_id} form)
    # statuses: results | empty | error
    # The success branch hands the STEP to the humanize_result composer; the
    # user-visible text is built in CODE by the presenter (#110) — the planner
    # does NOT write user_visible_summary or pick an output field. template_data
    # MUST be EXACTLY {intent, actions[{step_id}]} — no other top-level/action
    # key (the contract rejects them).
    # empty/error fall back to the safe canned generic_tool_error.
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="найди телефон ветклиники на Ленина",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "запрос свежей инфы из интернета — web_search",
            },
            "clarity": "clear",
            "actions": {
                "s1": {
                    "tool": "web_search",
                    "args": {"query": "телефон ветклиника Ленина"},
                    "expected_outcomes": [
                        {
                            "match": {"status": "results"},
                            "next": None,
                            "compose": {
                                "kind": "llm",
                                "llm_prompt_key": "humanize_result",
                                "template_data": {
                                    "intent": "найти телефон ветклиники",
                                    "actions": [{"step_id": "s1"}],
                                },
                            },
                        },
                        {
                            "match": {"status": "empty"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "generic_tool_error",
                                "template_data": {},
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
                "kind": "llm",
                "llm_prompt_key": "humanize_result",
                "template_data": {
                    "intent": "найти телефон ветклиники",
                    "actions": [{"step_id": "s1"}],
                },
            },
        },
    ),
    # ──────────────────────────────────────────────────────────────────
    # 13. show_checklist → checklist_show / checklist_empty (Family B fix)
    # statuses: ok | empty | error
    # show_checklist returns ShowChecklistOk{title, items[{title,
    # item_status}]} / ShowChecklistEmpty{title}. The ``ok`` branch renders
    # the structured items via checklist_show; ``empty`` uses checklist_empty.
    # Field names ``title`` / ``items`` / ``item_status`` match the
    # output_model; a done item gets a ✅.
    #
    # show_checklist is a READ tool — the compose MUST reference the REAL
    # tool output (``${s1.title}`` / ``${s1.items}``), NOT literal list
    # contents. Hardcoding sample items here would teach the planner to
    # invent/fabricate checklist contents at plan time (a valid-but-wrong
    # regression). This is a TEMPLATE compose: structural ``${sN.field}`` refs are
    # still the right tool here — ``${s1.items}`` is a full-ref string →
    # ``resolve_refs`` preserves the list type, so the template iterates the REAL
    # items at run time. (Narration via ``humanize_result`` no longer uses
    # ``${sN.field}``; it references the step by ``{step_id}`` — #110 Phase 5.)
    # ──────────────────────────────────────────────────────────────────
    FewShotExample(
        user_message="покажи список дача",
        context_brief="",
        plan={
            "schema_version": 1,
            "turn_classification": {
                "is_new_turn": True,
                "reason": "показать пункты конкретного чек-листа",
            },
            "clarity": "clear",
            "actions": {
                "s1": {
                    "tool": "show_checklist",
                    "args": {"list_id_or_title": "дача"},
                    "expected_outcomes": [
                        {
                            "match": {"status": "ok"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "checklist_show",
                                "template_data": {
                                    "title": "${s1.title}",
                                    "items": "${s1.items}",
                                },
                            },
                        },
                        {
                            "match": {"status": "empty"},
                            "next": None,
                            "compose": {
                                "kind": "template",
                                "template_id": "checklist_empty",
                                "template_data": {"title": "${s1.title}"},
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
                "template_id": "checklist_show",
                "template_data": {
                    "title": "${s1.title}",
                    "items": "${s1.items}",
                },
            },
        },
    ),
]


# ---------------------------------------------------------------------------
# PR-c / #143 Phase B: .only selector example (advertised to the planner).
# ---------------------------------------------------------------------------
# Appended to _EXAMPLES below so render_few_shot_block() actually teaches the
# planner the .only pattern (Codex PR-c R1: prose-only advertising without an
# example is half-baked). s1 MUST have a terminal empty branch; .only only in
# args, not compose. Validated by the few-shot contract tests.
#
# #143 Phase B (2026-06-14): пример переведён с reminders на ЧЕК-ЛИСТЫ «по
# описанию» — читающий list_checklist_items (s1) → ${s1.items.only.item_id} →
# mark_checklist_item_done (s2). Тот же канон .only, но демонстрирует id-путь
# чек-листов (у планировщика title-пути для mark/delete больше НЕТ). Замена, а
# не добавление — бюджет префикса (#128) headroom мал; reminders.only учился
# тем же шаблоном. Компактно: только success-путь + обязательная empty-ветка
# (error падает в общий invalid-plan fallback).
_ONLY_SELECTOR_EXAMPLE = FewShotExample(
    user_message="отметь лопату в списке дача",
    context_brief="",
    plan={
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "отметить пункт чек-листа"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "list_checklist_items",
                # #122/#143: выбор по имени — фильтр аргументом читающего шага,
                # дальше штатный .only по уже отфильтрованным кандидатам.
                "args": {"title_match": "лопата", "list_title_match": "дача"},
                "expected_outcomes": [
                    {"match": {"status": "ok"}, "next": "s2"},
                    {"match": {"status": "empty"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "checklist_items_empty",
                                 "template_data": {}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
            "s2": {
                "tool": "mark_checklist_item_done",
                "args": {"item_id": "${s1.items.only.item_id}"},
                "expected_outcomes": [
                    # Успех — без своего compose: падает в root humanize_result.
                    {"match": {"status": "done"}, "next": None},
                    # #143 Phase B (Codex CRITICAL): stale/исчезнувший
                    # item_id → status=error. Без этой ветки executor не
                    # нашёл бы branch → unknown_outcome → «поломка»/алерт
                    # (критерий приёмки #8). Мягкий id-free ответ.
                    {"match": {"status": "error"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "checklist_items_empty",
                                 "template_data": {}}},
                ],
                "intent_group": "default",
                "depends_on": ["s1"],
            },
        },
        "compose": {
            "kind": "llm",
            "llm_prompt_key": "humanize_result",
            "template_data": {
                "intent": "отметить пункт списка сделанным",
                "actions": [{"step_id": "s2"}],
            },
        },
    },
)

# Advertise the .only pattern to the planner (Codex PR-c R1).
_EXAMPLES.append(_ONLY_SELECTOR_EXAMPLE)


# ---------------------------------------------------------------------------
# #143 Phase B (Codex review MAJOR-5): positive .only пример для ЗАДАЧ.
# ---------------------------------------------------------------------------
# Исходный инцидент (#133, прод): «отмени задачу про интернет» →
# ${s1.items.only.task_id}, но у list_tasks поле ВЫДАЧИ — ``tasks``, не
# ``items`` → arg_violation на исполнении → «поломка». Чек-листовый .only
# пример учит item_id и мог КОСВЕННО закрепить неверный перенос (items) на
# задачи. Этот пример демонстрирует ПРАВИЛЬНОЕ поле ``tasks`` напрямую:
# list_tasks(date="all", title_match=…) → ${s1.tasks.only.task_id} →
# cancel_task. Компактно ради бюджета префикса (#128): success-путь + empty
# у продюсера (обязательно для .only) + error-ветка у мутатора (мягко).
_ONLY_SELECTOR_TASK_EXAMPLE = FewShotExample(
    user_message="отмени задачу про интернет",
    context_brief="",
    plan={
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "отменить задачу по описанию"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "list_tasks",
                # date="all" + title_match — фильтр аргументом ЧИТАЮЩЕГО шага
                # (#122), дальше .only по уже отфильтрованным кандидатам.
                # status опускаем — runtime default уже "pending".
                "args": {"date": "all", "title_match": "интернет"},
                "expected_outcomes": [
                    {"match": {"status": "ok"}, "next": "s2"},
                    # 0 совпадений — ОЖИДАЕМЫЙ исход «не нашла такую задачу»,
                    # НЕ «поломка» (критерий #8). Мягко через рот (humanize_result
                    # очеловечивает пустую выдачу s1), НЕ generic_tool_error
                    # (тот рендерится через breakdown pool = «поломка»/алерт).
                    {"match": {"status": "empty"}, "next": None,
                     "compose": {"kind": "llm",
                                 "llm_prompt_key": "humanize_result",
                                 "template_data": {
                                     "intent": "сообщить, что задача не найдена",
                                     "actions": [{"step_id": "s1"}]}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
            "s2": {
                "tool": "cancel_task",
                # ПОЛЕ ВЫДАЧИ list_tasks — ``tasks`` (НЕ items!).
                "args": {"task_id": "${s1.tasks.only.task_id}"},
                "expected_outcomes": [
                    # Успех — без своего compose: падает в root humanize_result
                    # (root ссылается на успешный s2). Экономит префикс (#128).
                    {"match": {"status": "cancelled"}, "next": None},
                    {"match": {"status": "error"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "generic_tool_error",
                                 "template_data": {}}},
                ],
                "intent_group": "default",
                "depends_on": ["s1"],
            },
        },
        "compose": {
            "kind": "llm",
            "llm_prompt_key": "humanize_result",
            "template_data": {
                "intent": "отменить задачу",
                "actions": [{"step_id": "s2"}],
            },
        },
    },
)

_EXAMPLES.append(_ONLY_SELECTOR_TASK_EXAMPLE)


# ---------------------------------------------------------------------------
# #144 (Задача 2): показ меню — маршрут list_menu + дружелюбный пустой исход.
# ---------------------------------------------------------------------------
# Прод-инцидент (#144, живой N=5): «покажи меню»/«поменяй обед» на ПУСТОМ меню
# давало мисроут («Не нашла рецепт „меню"», «список покупок пуст») или «поломку»,
# и лишь иногда верное «не составлено». Основной фикс — детерминированный override
# в composer (#144 Задача 1); этот few-shot — ПОДКРЕПЛЕНИЕ на стороне планировщика:
# учит, что «покажи меню» → ВСЕГДА list_menu (НЕ search_recipes/get_recipe), а
# пустой исход озвучивается через рот (humanize_result «не составлено, предложить
# составить»), НЕ через generic_tool_error (тот = «поломка»/алерт).
#
# Компактно ради бюджета префикса (#128): ветка ok БЕЗ своего compose — падает в
# root humanize_result (идентичен); явная только ветка empty (новое поведение).
# Зеркалит форму _ONLY_SELECTOR_TASK_EXAMPLE (empty-исход → humanize, не «поломка»).
_MENU_SHOW_EXAMPLE = FewShotExample(
    user_message="покажи меню",
    context_brief="",
    plan={
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "показать меню"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "list_menu",
                # week_start опускаем → runtime вернёт свежее меню (под «на эту
                # неделю» планировщик подставил бы понедельник текущей недели).
                "args": {},
                "expected_outcomes": [
                    # ok — без своего compose: падает в root humanize_result
                    # (root ссылается на s1). Экономит префикс (#128).
                    {"match": {"status": "ok"}, "next": None},
                    # ПУСТО — ОЖИДАЕМЫЙ исход «не составлено», НЕ «поломка»
                    # (#144). Мягко через рот: humanize_result сообщает, что
                    # меню не составлено, и предлагает составить — НЕ
                    # generic_tool_error (он рендерится «поломкой»/алертом).
                    {"match": {"status": "empty"}, "next": None,
                     "compose": {"kind": "llm",
                                 "llm_prompt_key": "humanize_result",
                                 "template_data": {
                                     "intent": "сообщить, что меню не составлено, предложить составить",
                                     "actions": [{"step_id": "s1"}]}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "llm",
            "llm_prompt_key": "humanize_result",
            "template_data": {
                "intent": "показать меню на неделю",
                "actions": [{"step_id": "s1"}],
            },
        },
    },
)

_EXAMPLES.append(_MENU_SHOW_EXAMPLE)


# ---------------------------------------------------------------------------
# #144 (Задача 1): ПРАВКА меню — preflight list_menu → update_menu_item.
# ---------------------------------------------------------------------------
# Прод-инцидент (#144, живой N=5): «поменяй обед в среду на суп» на ПУСТОМ меню.
# Планировщик строил preflight s1=list_menu → s2=update_menu_item, НО ветка empty
# у s1 не была терминальной → s2 пропускался (branch_not_selected) → план НЕ
# завершался чисто (outcome≠completed) → «поломка»; либо шёл прямо в update и
# ложно «Обновила». Этот пример учит ПРАВИЛЬНОЙ форме edit-плана:
#   - s1=list_menu с ДВУМЯ исходами: ok → next='s2' (меню есть → правим),
#     empty → ТЕРМИНАЛ (next=None) с дружелюбным humanize «не составлено,
#     предложить составить» — НЕ generic_tool_error (тот = «поломка»/алерт),
#     НЕ branch_not_selected. Это ключевой фикс (пустое меню НЕ ведёт в write).
#   - s2=update_menu_item ссылается на ПРАВИЛЬНОЕ поле выдачи list_menu —
#     ${s1.menu_id} (НЕ ${s1.plan_id}: поле list_menu называется menu_id,
#     валидатор C #143 отклонит plan_id с arg_ref_unknown_field).
# Branch-aware: ссылка ${s1.menu_id} в s2 валидируется против варианта ListMenuOk
# (ветка ok маршрутизирует в s2), где поле menu_id есть.
# Компактно ради бюджета префикса (#128): success-путь s2 БЕЗ своего compose —
# падает в root humanize_result (root ссылается на успешный s2); явные только
# empty у s1 (новое поведение) и error у s2 (мягко).
_MENU_EDIT_EXAMPLE = FewShotExample(
    user_message="поменяй обед в среду на суп",
    context_brief="",
    plan={
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "правка ячейки меню — preflight через list_menu"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "list_menu",
                # week_start опускаем → runtime вернёт свежее меню.
                "args": {},
                "expected_outcomes": [
                    # меню ЕСТЬ → правим (управление в s2).
                    {"match": {"status": "ok"}, "next": "s2"},
                    # ПУСТО → ТЕРМИНАЛ (#144): пустое меню НЕ ведёт в write.
                    # Мягко через рот: humanize_result сообщает, что меню не
                    # составлено, и предлагает составить — НЕ generic_tool_error
                    # (он = «поломка»/алерт), НЕ branch_not_selected.
                    {"match": {"status": "empty"}, "next": None,
                     "compose": {"kind": "llm",
                                 "llm_prompt_key": "humanize_result",
                                 "template_data": {
                                     "intent": "сообщить, что меню не составлено, предложить составить",
                                     "actions": [{"step_id": "s1"}]}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
            "s2": {
                "tool": "update_menu_item",
                # ПОЛЕ ВЫДАЧИ list_menu — ``menu_id`` (НЕ plan_id!).
                # day_of_week: 2 = среда (Пн=0); free_text вместо recipe_id.
                "args": {"plan_id": "${s1.menu_id}", "day_of_week": 2,
                         "meal_type": "lunch", "free_text": "суп"},
                "expected_outcomes": [
                    # Успех — без своего compose: падает в root humanize_result
                    # (root ссылается на успешный s2). Экономит префикс (#128).
                    {"match": {"status": "updated"}, "next": None},
                    {"match": {"status": "error"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "generic_tool_error",
                                 "template_data": {"error_code": "${s2.error_code}"}}},
                ],
                "intent_group": "default",
                "depends_on": ["s1"],
            },
        },
        "compose": {
            "kind": "llm",
            "llm_prompt_key": "humanize_result",
            "template_data": {
                "intent": "подтвердить, что обед в среду заменён",
                "actions": [{"step_id": "s2"}],
            },
        },
    },
)

_EXAMPLES.append(_MENU_EDIT_EXAMPLE)


# ---------------------------------------------------------------------------
# PR-d (Piece 3): INVALID-case examples — "так НЕ делай"
# ---------------------------------------------------------------------------
# Shown to the planner as a SEPARATE "do NOT do this" block — deliberately NOT
# added to ``_EXAMPLES`` (which is the taught-as-valid set the contract tests
# in test_planner_prompt_builder.py iterate and assert pass validate_plan).
# Each entry pairs a one-line "почему нельзя" with the validator violation it
# triggers, so the model learns the boundary, not a pattern to copy.
#
# These are intentionally small fragments (not full schema-valid Plans) — a
# couple are not even Plan-parsable (that IS the point). The invalid-case GUARD
# tests in test_planner_validator.py build the corresponding full plans and
# assert validate_plan rejects each with the named violation.


@dataclass(frozen=True)
class InvalidExample:
    """One anti-pattern. ``bad_fragment`` is a short JSON-ish snippet shown to
    the planner; ``why`` is a one-line reason; ``violation`` is the validator
    code (or schema rule) the full form triggers."""

    title: str
    bad_fragment: str
    why: str
    violation: str


_INVALID_EXAMPLES: list[InvalidExample] = [
    InvalidExample(
        title="смолток в clear",
        bad_fragment=(
            '{"clarity": "clear", "actions": {}, '
            '"compose": {"kind": "template", "template_id": "smalltalk_fallback", '
            '"template_data": {}}}'
        ),
        why=(
            "«привет»/«спасибо» — это reply_only с пустым actions, а не clear. "
            "В clear обязателен хотя бы один шаг, и разговорные шаблоны "
            "(smalltalk_fallback/identity_playful) там запрещены."
        ),
        violation="schema: Plan(clarity='clear') requires at least one action / "
        "conversational template only valid for reply_only",
    ),
    InvalidExample(
        title="выдуманный filter()/where() в ссылке",
        bad_fragment=(
            '{"item_ids": "${s1.items.filter('
            "title_contains='масло').only.item_id}\"}"
        ),
        why=(
            "filter()/where() в ${...}-ссылках НЕ существует. Фильтруй "
            "аргументом ЧИТАЮЩЕГО шага: list_shopping(title_match='масло'), "
            "затем `[\"${s1.items.only.item_id}\"]` — списковые аргументы "
            "оборачивай в [...]. (Прод-инцидент 2026-06-10.)"
        ),
        violation="invalid_ref_syntax",
    ),
    InvalidExample(
        title="индексная ссылка [0]",
        bad_fragment='{"reminder_id": "${s1.items[0].reminder_id}"}',
        why=(
            "Грамматика ссылок НЕ поддерживает скобочную индексацию `[0]`. "
            "Чтобы выбрать единственный элемент списка, используй селектор "
            "`.only` (например `${s1.items.only.reminder_id}`) и добавь шагу-"
            "источнику терминальную ветку status='empty'."
        ),
        violation="invalid_ref_syntax",
    ),
    InvalidExample(
        title="выдуманное выходное поле",
        bad_fragment=(
            '{"compose": {"template_id": "shopping_added_ok", '
            '"template_data": {"items": "${s1.items}"}}}  '
            "// add_shopping_items НЕ возвращает поле items"
        ),
        why=(
            "Нельзя ссылаться на поле, которого нет в output_model инструмента. "
            "У add_shopping_items есть added_count / item_ids, но НЕТ items. "
            "Сверяйся со списком полей в карточке инструмента."
        ),
        violation="compose_ref_unknown_field",
    ),
    InvalidExample(
        title=".only в compose",
        bad_fragment=(
            '{"compose": {"template_id": "reminder_set_ok", '
            '"template_data": {"what": "${s1.items.only.title}"}}}'
        ),
        why=(
            "`.only` допустим ТОЛЬКО в args шага (до вызова инструмента), где "
            "неоднозначность безопасно прерывает план. В compose он ЗАПРЕЩён: "
            "compose рендерится ПОСЛЕ записи, откатить уже нельзя. Ссылайся на "
            "уже разрешённое поле напрямую (например `${s2.reminder_id}`)."
        ),
        violation="only_selector_in_compose",
    ),
]


def render_invalid_examples_block(*, indent: int = 2) -> str:
    """Render the INVALID-case anti-patterns as a markdown "так НЕ делай"
    block for the planner prompt. Deterministic (prompt-cache safe).

    Kept separate from ``render_few_shot_block`` content shape: the valid
    examples teach plan shape; this block teaches the four boundaries the
    validator enforces (PR-d Piece 3). Appended by ``render_few_shot_block``
    so the single ``few_shot_block`` prompt slot carries both."""
    parts: list[str] = [
        "### ПРИМЕРЫ ОШИБОК — так НЕ делай",
        "",
        "Ниже — частые неверные планы. НЕ копируй их; каждый отвергается "
        "валидатором.",
        "",
    ]
    for n, ex in enumerate(_INVALID_EXAMPLES, start=1):
        parts.append(f"#### Ошибка {n}: {ex.title}")
        parts.append(f"НЕВЕРНО: {ex.bad_fragment}")
        parts.append(f"Почему нельзя: {ex.why}")
        parts.append(f"Нарушение: {ex.violation}")
        parts.append("")
    return "\n".join(parts).rstrip()


def all_invalid_examples() -> tuple[InvalidExample, ...]:
    """Public accessor for the invalid-case anti-patterns — used by the
    invalid-case guard tests so they assert against the same set the prompt
    teaches."""
    return tuple(_INVALID_EXAMPLES)


def _iter_example_composes(plan: dict) -> list[dict]:
    """Yield every compose dict in a plan: the plan-level ``compose`` plus
    each ``actions[*].expected_outcomes[*].compose``. Used to discover
    which LLM-prompt keys an example would teach the planner to emit."""
    composes: list[dict] = []
    root = plan.get("compose")
    if isinstance(root, dict):
        composes.append(root)
    actions = plan.get("actions")
    if isinstance(actions, dict):
        for step in actions.values():
            if not isinstance(step, dict):
                continue
            for outcome in step.get("expected_outcomes", []) or []:
                if not isinstance(outcome, dict):
                    continue
                c = outcome.get("compose")
                if isinstance(c, dict):
                    composes.append(c)
    return composes


def _example_referenced_llm_keys(plan: dict) -> set[str]:
    """All ``llm_prompt_key`` values used by any ``kind="llm"`` compose in
    the example (plan-level or branch). Empty set ⇒ template-only example."""
    keys: set[str] = set()
    for compose in _iter_example_composes(plan):
        if compose.get("kind") == "llm":
            key = compose.get("llm_prompt_key")
            if isinstance(key, str) and key:
                keys.add(key)
    return keys


def render_few_shot_block(
    *,
    indent: int = 2,
    effective_llm_keys: Iterable[str] | None = None,
) -> str:
    """Render examples as a markdown block for the system prompt.

    Output format:
    ```
    ### Пример 1: <user_message>
    Контекст: <brief>
    План:
    <pretty-printed JSON>
    ```

    Used by `prompt_builder.build_cached_prefix(...)`. Returns deterministic
    string so prompt cache hits across requests.

    ``effective_llm_keys`` (rot-enablement #88, Codex Phase-2 R1 MAJOR-1):
    the GATED set of composer LLM-prompt keys actually enabled for this
    run (caller-proposed ∩ registry ∩ settings-enabled — same set the
    orchestrator renders into ``composer_llm_prompt_keys_block`` and the
    validator enforces). When provided, any example that teaches a
    ``kind="llm"`` compose whose ``llm_prompt_key`` is NOT in this set is
    DROPPED — otherwise a gate-off run would still teach the planner to
    emit (e.g.) ``reply_only+smalltalk``, which the validator then rejects
    as ``unknown_llm_prompt_key``, turning greetings into invalid-plan
    fallbacks. ``None`` (default) renders ALL examples (back-compat for
    callers that don't gate, e.g. legacy tests)."""
    gate: set[str] | None = (
        None if effective_llm_keys is None else set(effective_llm_keys)
    )
    parts: list[str] = []
    shown = 0
    for ex in _EXAMPLES:
        if gate is not None:
            needed = _example_referenced_llm_keys(ex.plan)
            if needed - gate:
                # Example teaches an LLM key not enabled for this run → skip.
                continue
        shown += 1
        parts.append(f"### Пример {shown}: {ex.user_message}")
        if ex.context_brief:
            parts.append(f"Контекст: {ex.context_brief}")
        parts.append("План:")
        parts.append(
            json.dumps(ex.plan, ensure_ascii=False, indent=indent, sort_keys=False)
        )
        parts.append("")  # blank line between examples
    # PR-d (Piece 3): append the INVALID-case "так НЕ делай" block so the
    # single few_shot prompt slot teaches both the valid shapes and the four
    # boundaries the validator enforces. The gate above only filters VALID
    # LLM-key examples; the invalid block has no kind='llm' composes so it is
    # always shown.
    parts.append(render_invalid_examples_block(indent=indent))
    return "\n".join(parts).rstrip()


def example_count() -> int:
    """Public counter — used by snapshot tests."""
    return len(_EXAMPLES)


def all_examples() -> tuple[FewShotExample, ...]:
    """Public accessor for examples — used by validation tests so they
    can iterate without exposing the private list directly."""
    return tuple(_EXAMPLES)


__all__ = [
    "FewShotExample",
    "InvalidExample",
    "all_examples",
    "all_invalid_examples",
    "example_count",
    "render_few_shot_block",
    "render_invalid_examples_block",
]
