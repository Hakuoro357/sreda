"""Tests for planner schemas (Sub-A1 from Epic #74).

Covers Plan / Action / OutcomeBranch / ComposerCall / TurnClassification.
Pydantic v2 strict mode (extra='forbid') is part of the contract — extra
fields must raise ValidationError so a planner that drifts beyond the
contract is caught immediately, not silently accepted.

Plan-level validation responsibilities (verified here):
- ``depends_on`` references unknown action -> ValidationError
- ``depends_on`` references self -> ValidationError
- ``depends_on`` contains a cycle (a -> b -> a) -> ValidationError
- ``expected_outcomes[].next`` references unknown action -> ValidationError

Implicit-ref cycle detection (via ``${node.field}`` in args) is
deliberately out of scope here; that lives in the Phase B validator.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sreda.runtime.planner.schemas import (
    Action,
    ComposerCall,
    OutcomeBranch,
    Plan,
    TurnClassification,
)


# ---------------------------------------------------------------------------
# TurnClassification
# ---------------------------------------------------------------------------


def test_turn_classification_valid() -> None:
    tc = TurnClassification(is_new_turn=True, reason="новая тема — рецепт")
    assert tc.is_new_turn is True
    assert tc.reason == "новая тема — рецепт"


def test_turn_classification_rejects_empty_reason() -> None:
    with pytest.raises(ValidationError):
        TurnClassification(is_new_turn=True, reason="")


def test_turn_classification_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        TurnClassification(is_new_turn=True, reason="ok", extra_field="nope")


# ---------------------------------------------------------------------------
# ComposerCall
# ---------------------------------------------------------------------------


def test_composer_call_template_kind_requires_template_id() -> None:
    with pytest.raises(ValidationError):
        ComposerCall(kind="template")


def test_composer_call_template_kind_with_id_ok() -> None:
    cc = ComposerCall(
        kind="template",
        template_id="add_items_success",
        template_data={"items": ["хлеб"]},
    )
    assert cc.kind == "template"
    assert cc.template_id == "add_items_success"


def test_composer_call_llm_kind_requires_llm_prompt_key() -> None:
    with pytest.raises(ValidationError):
        ComposerCall(kind="llm")


def test_composer_call_llm_kind_with_key_ok() -> None:
    cc = ComposerCall(kind="llm", llm_prompt_key="recipe_narrator_v1")
    assert cc.kind == "llm"
    assert cc.llm_prompt_key == "recipe_narrator_v1"


def test_composer_call_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        ComposerCall(
            kind="template", template_id="x", template_data={}, unknown="value"
        )


# ---------------------------------------------------------------------------
# OutcomeBranch
# ---------------------------------------------------------------------------


def test_outcome_branch_with_next_ok() -> None:
    branch = OutcomeBranch(match={"status": "found"}, next="s2")
    assert branch.next == "s2"
    assert branch.compose is None


def test_outcome_branch_with_compose_ok() -> None:
    branch = OutcomeBranch(
        match={"status": "added"},
        compose=ComposerCall(kind="template", template_id="add_ok"),
    )
    assert branch.compose is not None
    assert branch.next is None


def test_outcome_branch_with_both_next_and_compose_rejected() -> None:
    # Ambiguous: should the executor jump to next or run compose?
    with pytest.raises(ValidationError):
        OutcomeBranch(
            match={"status": "found"},
            next="s2",
            compose=ComposerCall(kind="template", template_id="x"),
        )


def test_outcome_branch_open_end_when_neither_next_nor_compose() -> None:
    # Terminal branch that defers to Plan.compose — must be allowed,
    # this is the common case when planner sets plan-level compose.
    branch = OutcomeBranch(match={"status": "added"})
    assert branch.next is None
    assert branch.compose is None


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------


def test_action_minimal_ok() -> None:
    action = Action(
        tool="list_shopping",
        args={},
        expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
    )
    assert action.tool == "list_shopping"
    assert action.intent_group == "default"  # default value
    assert action.depends_on == []


def test_action_rejects_empty_expected_outcomes() -> None:
    # An action with no branches is meaningless — planner forgot to list outcomes
    with pytest.raises(ValidationError):
        Action(tool="list_shopping", args={}, expected_outcomes=[])


def test_action_rejects_empty_tool_name() -> None:
    with pytest.raises(ValidationError):
        Action(
            tool="",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
        )


def test_action_rejects_non_string_intent_group() -> None:
    with pytest.raises(ValidationError):
        Action(
            tool="x",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
            intent_group=123,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Plan — happy paths
# ---------------------------------------------------------------------------


def _ok_action(tool: str = "list_shopping") -> Action:
    return Action(
        tool=tool,
        args={},
        expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
    )


def _ok_compose() -> ComposerCall:
    return ComposerCall(kind="template", template_id="shopping_ok")


def _ok_tc() -> TurnClassification:
    return TurnClassification(is_new_turn=True, reason="новый запрос")


def test_plan_with_single_action_ok() -> None:
    plan = Plan(
        turn_classification=_ok_tc(),
        actions={"s1": _ok_action()},
        compose=_ok_compose(),
    )
    assert len(plan.actions) == 1
    assert plan.schema_version == 1


def test_plan_with_ten_actions_ok() -> None:
    actions = {f"s{i}": _ok_action() for i in range(10)}
    plan = Plan(
        turn_classification=_ok_tc(),
        actions=actions,
        compose=_ok_compose(),
    )
    assert len(plan.actions) == 10


# ---------------------------------------------------------------------------
# Plan — validation errors
# ---------------------------------------------------------------------------


def test_plan_rejects_empty_actions() -> None:
    with pytest.raises(ValidationError):
        Plan(
            turn_classification=_ok_tc(),
            actions={},
            compose=_ok_compose(),
        )


def test_plan_rejects_missing_turn_classification() -> None:
    with pytest.raises(ValidationError):
        Plan(actions={"s1": _ok_action()}, compose=_ok_compose())  # type: ignore[call-arg]


def test_plan_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        Plan(
            turn_classification=_ok_tc(),
            actions={"s1": _ok_action()},
            compose=_ok_compose(),
            mystery_field="ignore me",
        )


def test_plan_depends_on_unknown_action_rejected() -> None:
    actions = {
        "s1": Action(
            tool="list_shopping",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
            depends_on=["s_nonexistent"],
        )
    }
    with pytest.raises(ValidationError) as exc:
        Plan(turn_classification=_ok_tc(), actions=actions, compose=_ok_compose())
    assert "s_nonexistent" in str(exc.value)


def test_plan_depends_on_self_rejected() -> None:
    actions = {
        "s1": Action(
            tool="list_shopping",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
            depends_on=["s1"],
        )
    }
    with pytest.raises(ValidationError) as exc:
        Plan(turn_classification=_ok_tc(), actions=actions, compose=_ok_compose())
    assert "s1" in str(exc.value)


def test_plan_depends_on_cycle_rejected() -> None:
    # s1 -> s2 -> s1: classic cycle
    actions = {
        "s1": Action(
            tool="list_shopping",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
            depends_on=["s2"],
        ),
        "s2": Action(
            tool="list_reminders",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
            depends_on=["s1"],
        ),
    }
    with pytest.raises(ValidationError) as exc:
        Plan(turn_classification=_ok_tc(), actions=actions, compose=_ok_compose())
    msg = str(exc.value).lower()
    assert "cycle" in msg or "цикл" in msg


def test_plan_branch_next_to_unknown_action_rejected() -> None:
    actions = {
        "s1": Action(
            tool="list_shopping",
            args={},
            expected_outcomes=[
                OutcomeBranch(match={"status": "ok"}, next="s_ghost"),
            ],
        )
    }
    with pytest.raises(ValidationError) as exc:
        Plan(turn_classification=_ok_tc(), actions=actions, compose=_ok_compose())
    assert "s_ghost" in str(exc.value)


# ---------------------------------------------------------------------------
# Plan — full realistic example
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Plan — Code-review 2026-05-25 follow-ups
# ---------------------------------------------------------------------------


def test_plan_depends_on_duplicate_rejected() -> None:
    """``depends_on=['s2','s2']`` is never useful and breaks Kahn's math.

    Code-reviewer MAJOR #2 — silent dedup would violate the
    no-silent-acceptance principle, so we explicitly reject.
    """
    actions = {
        "s1": Action(
            tool="list_shopping",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
            depends_on=["s2", "s2"],
        ),
        "s2": _ok_action(),
    }
    with pytest.raises(ValidationError) as exc:
        Plan(turn_classification=_ok_tc(), actions=actions, compose=_ok_compose())
    msg = str(exc.value).lower()
    assert "duplicate" in msg or "дубл" in msg


def test_plan_branch_next_self_loop_rejected() -> None:
    """``branch.next == action_id`` would deadloop the executor.

    Code-reviewer MINOR #5 — depends_on self-ref is already caught,
    branch.next self-ref should be too (symmetric invariant).
    """
    actions = {
        "s1": Action(
            tool="list_shopping",
            args={},
            expected_outcomes=[
                OutcomeBranch(match={"status": "ok"}, next="s1"),
            ],
        ),
    }
    with pytest.raises(ValidationError) as exc:
        Plan(turn_classification=_ok_tc(), actions=actions, compose=_ok_compose())
    msg = str(exc.value).lower()
    assert "s1" in msg
    assert "loop" in msg or "itself" in msg or "infinite" in msg or "цикл" in msg


def test_plan_depends_on_three_node_cycle_rejected() -> None:
    """3-node cycle s1 → s2 → s3 → s1.

    Code-reviewer MINOR #7 — exercises a different Kahn's code path
    than the 2-node test (more BFS iterations before detecting
    leftover nodes).
    """
    actions = {
        "s1": Action(
            tool="list_shopping",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
            depends_on=["s2"],
        ),
        "s2": Action(
            tool="list_reminders",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
            depends_on=["s3"],
        ),
        "s3": Action(
            tool="list_tasks",
            args={},
            expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
            depends_on=["s1"],
        ),
    }
    with pytest.raises(ValidationError) as exc:
        Plan(turn_classification=_ok_tc(), actions=actions, compose=_ok_compose())
    msg = str(exc.value).lower()
    assert "cycle" in msg or "цикл" in msg


def test_plan_realistic_recipe_to_shopping() -> None:
    """Real housewife scenario: «Найди рецепт борща и добавь ингредиенты в покупки»."""
    actions = {
        "s1": Action(
            tool="get_recipe_any_source",
            args={"query": "борщ"},
            expected_outcomes=[
                OutcomeBranch(match={"status": "found"}, next="s2"),
                OutcomeBranch(
                    match={"status": "not_available"},
                    compose=ComposerCall(
                        kind="template",
                        template_id="recipe_not_found_ask_alt",
                        template_data={"query": "борщ"},
                    ),
                ),
            ],
        ),
        "s2": Action(
            tool="add_shopping_items",
            args={"items": "${s1.recipe.ingredients}"},
            expected_outcomes=[
                OutcomeBranch(match={"status": "added"}),
                OutcomeBranch(match={"status": "partial_duplicate"}),
            ],
            depends_on=["s1"],
        ),
    }
    plan = Plan(
        turn_classification=TurnClassification(
            is_new_turn=True,
            reason="новая тема — рецепт + покупки",
        ),
        actions=actions,
        compose=ComposerCall(
            kind="template",
            template_id="recipe_added_to_shopping",
            template_data={
                "recipe": "${s1.recipe.title}",
                "added": "${s2.added_count}",
            },
        ),
    )
    assert len(plan.actions) == 2
    assert plan.actions["s2"].depends_on == ["s1"]


# ---------------------------------------------------------------------------
# Plan.clarity contract (vex-assistant#77 item #2)
# ---------------------------------------------------------------------------


def test_plan_clarity_defaults_to_clear() -> None:
    """Existing plans (no clarity field) parse as clarity='clear'.
    Backward-compat guarantee for already-deployed planner output."""
    plan = Plan(
        turn_classification=_ok_tc(),
        actions={"s1": _ok_action()},
        compose=_ok_compose(),
    )
    assert plan.clarity == "clear"
    assert plan.clarity_reason is None


def test_plan_clarity_clear_with_empty_actions_rejected() -> None:
    """clarity='clear' + empty actions = no-op plan; nonsense for the
    executor. Must reject — caller should set
    clarity='needs_clarification' instead."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="clear",
            actions={},
            compose=_ok_compose(),
        )
    assert "at least one action" in str(exc.value).lower()


def test_plan_needs_clarification_with_empty_actions_accepted() -> None:
    """The point of clarity='needs_clarification' — planner caught
    ambiguity, wants to ask back without dispatching tools."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="не указано время напоминания",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
        ),
    )
    assert plan.clarity == "needs_clarification"
    assert plan.clarity_reason == "не указано время напоминания"
    assert plan.actions == {}


def test_plan_needs_clarification_requires_reason() -> None:
    """The composer template needs clarity_reason to render the ask.
    Empty/None/whitespace-only all rejected."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            # clarity_reason omitted entirely
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="ask_user_for_clarification",
            ),
        )
    assert "clarity_reason" in str(exc.value)


def test_plan_needs_clarification_blank_reason_rejected() -> None:
    """Whitespace-only reason is the same as missing — composer
    can't render a useful ask from empty text."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="   ",
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="ask_user_for_clarification",
            ),
        )
    assert "clarity_reason" in str(exc.value).lower()


def test_plan_mixed_mode_with_ask_when_to_remind_rejected() -> None:
    """Codex Sub-A-77 #2 R2 MAJOR #1 — narrow ask templates like
    ``ask_when_to_remind`` only render the question; they don't
    acknowledge completed actions. Mixed mode (clarity=needs_clarification
    + non-empty actions) MUST use partial_with_clarification."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="не указано время для напоминания",
            actions={
                "s1": Action(
                    tool="add_shopping_items",
                    args={"items": ["молоко"]},
                    expected_outcomes=[OutcomeBranch(match={"status": "added"})],
                ),
            },
            compose=ComposerCall(
                kind="template",
                template_id="ask_when_to_remind",
                template_data={"what": "позвонить врачу"},
            ),
        )
    msg = str(exc.value)
    assert "partial_with_clarification" in msg


def test_plan_mixed_mode_with_ask_user_for_clarification_rejected() -> None:
    """Same as above for the generic ask template — it doesn't show
    done_summary either, so mixed mode forbids it."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="что-то неясное",
            actions={
                "s1": Action(
                    tool="add_shopping_items",
                    args={"items": ["хлеб"]},
                    expected_outcomes=[OutcomeBranch(match={"status": "added"})],
                ),
            },
            compose=ComposerCall(
                kind="template",
                template_id="ask_user_for_clarification",
                template_data={"missing_fields": ["recipient"]},
            ),
        )
    msg = str(exc.value)
    assert "partial_with_clarification" in msg


def test_plan_clarity_invalid_literal_rejected() -> None:
    """clarity is a closed enum — typos like 'unclear' or 'partial'
    must be caught at parse time."""
    with pytest.raises(ValidationError):
        Plan(
            turn_classification=_ok_tc(),
            clarity="partially_clear",  # invalid literal
            actions={"s1": _ok_action()},
            compose=_ok_compose(),
        )


def test_plan_clarity_reason_max_length_enforced() -> None:
    """500-char ceiling matches turn_classification.reason — planner
    should give a SHORT explanation, not a paragraph. Long reason
    would mean the planner is using clarity_reason as a fallback
    response channel (wrong layer)."""
    with pytest.raises(ValidationError):
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="x" * 501,
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="ask_user_for_clarification",
            ),
        )


def test_plan_clarity_reason_with_clear_is_allowed_but_unused() -> None:
    """A planner might emit clarity_reason for telemetry even when
    clarity='clear'. We don't error — just don't surface it. Forward
    compatibility for future "soft clarification" telemetry feature."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="clear",
        clarity_reason="for telemetry only — request was actually clear",
        actions={"s1": _ok_action()},
        compose=_ok_compose(),
    )
    assert plan.clarity == "clear"
    assert plan.clarity_reason is not None


# ---------------------------------------------------------------------------
# Plan.clarity stricter validation — Codex Sub-A-77 #2 R1 fixes
# ---------------------------------------------------------------------------


def test_plan_needs_clarification_with_llm_compose_rejected() -> None:
    """LLM-path compose would let the model free-text its own reply,
    undermining the deterministic clarification ask. Must reject."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="не указано время",
            actions={},
            compose=ComposerCall(
                kind="llm",
                llm_prompt_key="generic_followup",
            ),
        )
    assert "kind" in str(exc.value)


def test_plan_needs_clarification_with_non_clarification_template_rejected() -> None:
    """Pointing the composer at a success/error template while
    claiming needs_clarification = lying to the executor. Allowlist
    catches it (Codex R1 MAJOR #2)."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="что-то непонятное",
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="recipe_show",  # success template, not an ask
            ),
        )
    msg = str(exc.value)
    assert "template_id" in msg
    # Allowlist contents leaked into error message for caller hints.
    assert "ask_user_for_clarification" in msg


def test_plan_needs_clarification_with_partial_with_clarification_accepted() -> None:
    """Mixed-mode template — explicitly added to handle the "some
    actions ran, others need clarification" case the simpler
    ask_when_to_remind doesn't acknowledge."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="не указано время для напоминания",
        actions={
            "s1": Action(
                tool="add_shopping_items",
                args={"items": ["молоко"]},
                expected_outcomes=[OutcomeBranch(match={"status": "added"})],
            ),
        },
        compose=ComposerCall(
            kind="template",
            template_id="partial_with_clarification",
            template_data={
                "done_summary": "добавила молоко в покупки",
                "missing_fields": ["time"],
            },
        ),
    )
    assert plan.compose.template_id == "partial_with_clarification"


def test_plan_clarity_reason_auto_merged_into_template_data() -> None:
    """Codex R1 MAJOR #1 — if planner sets Plan.clarity_reason but
    forgets compose.template_data['clarity_reason'], schema fills it
    in automatically so composer template doesn't fall back to the
    generic «Не до конца поняла запрос»."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="не указано время напоминания",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={"missing_fields": ["time"]},  # no clarity_reason
        ),
    )
    assert plan.compose.template_data["clarity_reason"] == (
        "не указано время напоминания"
    )


def test_plan_caller_can_override_clarity_reason_in_template_data() -> None:
    """Auto-merge is a SOFT default — if caller wants a softer
    user-facing phrasing distinct from the machine-facing internal
    reason, their value wins."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="missing required argument time_iso",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={
                "clarity_reason": "не успела расслышать время",
                "missing_fields": ["time"],
            },
        ),
    )
    # Caller override preserved, NOT overwritten with the machine-facing
    # Plan-level reason.
    assert plan.compose.template_data["clarity_reason"] == (
        "не успела расслышать время"
    )
    # Plan-level reason still available for telemetry / GEPA.
    assert plan.clarity_reason == "missing required argument time_iso"


def test_clarification_template_ids_constant_shape() -> None:
    """CLARIFICATION_TEMPLATE_IDS is the single source of truth —
    keep it as a frozenset so callers can't accidentally mutate it
    at runtime."""
    from sreda.runtime.planner.schemas import CLARIFICATION_TEMPLATE_IDS
    assert isinstance(CLARIFICATION_TEMPLATE_IDS, frozenset)
    # Specific elements verified — if anyone removes one, CI catches it.
    assert "ask_user_for_clarification" in CLARIFICATION_TEMPLATE_IDS
    assert "ask_when_to_remind" in CLARIFICATION_TEMPLATE_IDS
    assert "partial_with_clarification" in CLARIFICATION_TEMPLATE_IDS


# ---------------------------------------------------------------------------
# Codex R2 follow-ups — blank-bypass + per-template required-fields
# ---------------------------------------------------------------------------


def test_plan_auto_merge_clarity_reason_when_blank_string() -> None:
    """Codex R2 MAJOR #2 — auto-merge must fire when caller provided
    an empty string for clarity_reason, not only when key is absent.
    Otherwise empty/whitespace bypasses the merge → composer falls
    back to the generic «Не до конца поняла» opener."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="не указано время",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={"clarity_reason": "", "missing_fields": ["time"]},
        ),
    )
    # Empty string → schema fills with the Plan-level reason.
    assert plan.compose.template_data["clarity_reason"] == "не указано время"


def test_plan_auto_merge_clarity_reason_when_whitespace() -> None:
    """Whitespace-only value treated the same as empty."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="ambig X",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={"clarity_reason": "   ", "missing_fields": ["time"]},
        ),
    )
    assert plan.compose.template_data["clarity_reason"] == "ambig X"


def test_plan_ask_when_to_remind_requires_what_field() -> None:
    """Codex R2 new MAJOR — narrow templates have required template_data
    fields; without them StrictUndefined crashes at render time. Schema
    catches it at parse time instead."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="не указано время",
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="ask_when_to_remind",
                # missing "what" — required by template
                template_data={},
            ),
        )
    msg = str(exc.value)
    assert "ask_when_to_remind" in msg
    assert "what" in msg


def test_plan_ask_when_to_remind_blank_what_rejected() -> None:
    """Blank string for required field == missing for this contract."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="не указано время",
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="ask_when_to_remind",
                template_data={"what": "   "},
            ),
        )
    assert "what" in str(exc.value)


def test_plan_partial_with_clarification_requires_done_summary() -> None:
    """Mixed-mode template needs done_summary to acknowledge completed
    work; without it the user wouldn't know any action ran."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="что-то неясно",
            actions={
                "s1": Action(
                    tool="add_shopping_items",
                    args={"items": ["молоко"]},
                    expected_outcomes=[OutcomeBranch(match={"status": "added"})],
                ),
            },
            compose=ComposerCall(
                kind="template",
                template_id="partial_with_clarification",
                # missing "done_summary"
                template_data={"missing_fields": ["time"]},
            ),
        )
    msg = str(exc.value)
    assert "partial_with_clarification" in msg
    assert "done_summary" in msg


def test_plan_ask_user_for_clarification_has_no_required_fields() -> None:
    """Generic ask template has fallbacks for every variable — schema
    must NOT impose required-field rules on it. Empty template_data
    is valid (composer template renders generic opener + fallback
    question)."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="запрос непонятен",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={},
        ),
    )
    # Auto-merge still injects clarity_reason for renderer convenience.
    assert plan.compose.template_data["clarity_reason"] == "запрос непонятен"


# ---------------------------------------------------------------------------
# Codex R3 follow-ups — strict string-type checks + allowlist coverage
# ---------------------------------------------------------------------------


def test_plan_clarity_reason_non_string_replaced_by_merge() -> None:
    """Codex R3 MAJOR — template_data is dict[str, Any] so caller
    could pass [], False, 0 etc. for clarity_reason. Pure
    truthiness lets [] / 0 / False through; pure str-check lets
    blank "" through. Schema requires non-blank STRING; if not,
    auto-merge replaces with Plan-level reason."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="proper reason",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={"clarity_reason": [], "missing_fields": ["time"]},
        ),
    )
    # List replaced by string from Plan-level.
    assert plan.compose.template_data["clarity_reason"] == "proper reason"


@pytest.mark.parametrize(
    "bad_value",
    [
        None,
        "",
        "   ",
        [],
        {},
        False,
        0,
        123,
        ["what?"],
    ],
    ids=["none", "empty", "whitespace", "empty_list", "empty_dict", "false", "zero", "int", "list_of_str"],
)
def test_plan_ask_when_to_remind_what_must_be_non_blank_string(bad_value) -> None:
    """Required template_data fields must be non-blank STRINGS.
    Various non-string types and blank strings all rejected so
    StrictUndefined doesn't crash and renderer gets sane input."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="не указано время",
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="ask_when_to_remind",
                template_data={"what": bad_value},
            ),
        )
    assert "what" in str(exc.value)


def test_plan_done_summary_non_string_rejected() -> None:
    """Codex R3 MAJOR — same for partial_with_clarification's
    done_summary. List/bool/etc. must reject."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="нужно уточнить",
            actions={
                "s1": Action(
                    tool="add_shopping_items",
                    args={"items": ["молоко"]},
                    expected_outcomes=[OutcomeBranch(match={"status": "added"})],
                ),
            },
            compose=ComposerCall(
                kind="template",
                template_id="partial_with_clarification",
                template_data={
                    "done_summary": ["item1", "item2"],  # list, not str
                    "missing_fields": ["time"],
                },
            ),
        )
    msg = str(exc.value)
    assert "done_summary" in msg


def test_clarification_required_fields_covers_allowlist() -> None:
    """Codex R3 MINOR — _TEMPLATE_REQUIRED_FIELDS must have an entry
    for every CLARIFICATION_TEMPLATE_IDS member, even if empty.
    Otherwise a future template added to the allowlist gets a free
    pass via the validator's .get(..., frozenset()) default —
    reintroducing schema-valid-but-render-invalid plans."""
    from sreda.runtime.planner.schemas import (
        CLARIFICATION_TEMPLATE_IDS,
        _TEMPLATE_REQUIRED_FIELDS,
    )
    assert set(_TEMPLATE_REQUIRED_FIELDS.keys()) == CLARIFICATION_TEMPLATE_IDS, (
        f"_TEMPLATE_REQUIRED_FIELDS keys {sorted(_TEMPLATE_REQUIRED_FIELDS.keys())} "
        f"must equal CLARIFICATION_TEMPLATE_IDS {sorted(CLARIFICATION_TEMPLATE_IDS)}. "
        f"Add a (possibly-empty) entry when extending the allowlist."
    )


def test_clarification_optional_str_list_fields_covers_allowlist() -> None:
    """Codex R4 MAJOR — _TEMPLATE_OPTIONAL_STR_LIST_FIELDS same
    coverage invariant: every CLARIFICATION_TEMPLATE_IDS member
    needs an explicit entry (even if empty)."""
    from sreda.runtime.planner.schemas import (
        CLARIFICATION_TEMPLATE_IDS,
        _TEMPLATE_OPTIONAL_STR_LIST_FIELDS,
    )
    assert (
        set(_TEMPLATE_OPTIONAL_STR_LIST_FIELDS.keys())
        == CLARIFICATION_TEMPLATE_IDS
    ), (
        f"_TEMPLATE_OPTIONAL_STR_LIST_FIELDS keys "
        f"{sorted(_TEMPLATE_OPTIONAL_STR_LIST_FIELDS.keys())} must equal "
        f"CLARIFICATION_TEMPLATE_IDS {sorted(CLARIFICATION_TEMPLATE_IDS)}."
    )


# ---------------------------------------------------------------------------
# Codex R4 follow-ups — optional list-of-strings field type checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        1,
        True,
        "time",  # string, not list-of-string
        {"time": True},  # dict
        42.5,
    ],
    ids=["int", "bool", "string", "dict", "float"],
)
def test_plan_missing_fields_non_list_rejected(bad_value) -> None:
    """Codex R4 MAJOR — ``missing_fields`` must be a list/tuple if
    present. Non-iterable or wrong-iterable type → schema-valid plan
    that crashes render or produces garbage (e.g. iterating string
    yields per-character bullets)."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="не указано время",
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="ask_user_for_clarification",
                template_data={"missing_fields": bad_value},
            ),
        )
    assert "missing_fields" in str(exc.value)


def test_plan_missing_fields_non_string_element_rejected() -> None:
    """List shape is right but element types wrong — must catch."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="что-то неясное",
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="ask_user_for_clarification",
                template_data={"missing_fields": ["time", 42, "items"]},
            ),
        )
    msg = str(exc.value)
    assert "missing_fields" in msg
    # Index of bad element surfaced for diagnostic clarity.
    assert "[1]" in msg


def test_plan_missing_fields_blank_element_rejected() -> None:
    """Blank string element invalid — would render an empty bullet line."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="неясно",
            actions={},
            compose=ComposerCall(
                kind="template",
                template_id="ask_user_for_clarification",
                template_data={"missing_fields": ["time", "   "]},
            ),
        )
    assert "missing_fields" in str(exc.value)


def test_plan_missing_fields_valid_list_accepted() -> None:
    """Sanity — proper list[str] of non-blank values goes through."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="не указано время",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={"missing_fields": ["time", "recipient"]},
        ),
    )
    assert plan.compose.template_data["missing_fields"] == ["time", "recipient"]


def test_plan_missing_fields_tuple_also_accepted() -> None:
    """Tuple is a valid Sequence — accept it too. Some planner
    output paths may emit tuples (e.g. through pydantic coercion)."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="не указано время",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={"missing_fields": ("time", "recipient")},
        ),
    )
    # Persisted as the tuple caller gave us; renderer iterates either way.
    assert tuple(plan.compose.template_data["missing_fields"]) == (
        "time",
        "recipient",
    )


def test_plan_missing_fields_omitted_no_check() -> None:
    """Optional — absence is fine. Template falls back to generic
    «Скажи чуть подробнее»."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="что-то неясное",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={},
        ),
    )
    assert "missing_fields" not in plan.compose.template_data


def test_plan_partial_with_clarification_missing_fields_validated_too() -> None:
    """Same check fires for partial_with_clarification — both templates
    iterate missing_fields the same way."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="нужно ещё уточнить",
            actions={
                "s1": Action(
                    tool="add_shopping_items",
                    args={"items": ["хлеб"]},
                    expected_outcomes=[OutcomeBranch(match={"status": "added"})],
                ),
            },
            compose=ComposerCall(
                kind="template",
                template_id="partial_with_clarification",
                template_data={
                    "done_summary": "записала хлеб",
                    "missing_fields": "time",  # string, not list
                },
            ),
        )
    assert "missing_fields" in str(exc.value)


def test_plan_ask_when_to_remind_missing_fields_not_checked() -> None:
    """ask_when_to_remind has empty entry in
    _TEMPLATE_OPTIONAL_STR_LIST_FIELDS — passing a weird
    missing_fields value is harmless because template doesn't use
    it. Validator should NOT reject."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="needs_clarification",
        clarity_reason="не указано время",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_when_to_remind",
            template_data={
                "what": "позвонить врачу",
                "missing_fields": 42,  # weird, but template doesn't use it
            },
        ),
    )
    # No exception — template_data preserved verbatim.
    assert plan.compose.template_data["missing_fields"] == 42
