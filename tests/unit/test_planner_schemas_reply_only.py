"""Tests for clarity='reply_only' schema (rot-enablement Phase 1, issue #88).

Covers:
- reply_only valid paths: LLM smalltalk/identity + template identity_playful
- reply_only invalid: non-conversational LLM prompt key
- reply_only invalid: non-conversational template id
- reply_only invalid: >=1 action (the "silent no-op" guard)
- clarity='clear' still requires >=1 action (regression)
- Semantic misclassification is a planner-prompt concern (documented test)
- Identity template denylist: must not contain real model/provider names
- Identity template non-empty
- smalltalk / humanize_result validate_data happy + reject paths
- CONVERSATIONAL_LLM_PROMPT_KEYS / CONVERSATIONAL_TEMPLATE_IDS constants
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from sreda.runtime.planner.schemas import (
    Action,
    ComposerCall,
    CONVERSATIONAL_LLM_PROMPT_KEYS,
    CONVERSATIONAL_TEMPLATE_IDS,
    OutcomeBranch,
    Plan,
    TurnClassification,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_action(tool: str = "list_shopping") -> Action:
    return Action(
        tool=tool,
        args={},
        expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
    )


def _ok_tc() -> TurnClassification:
    return TurnClassification(is_new_turn=True, reason="новый запрос")


def _reply_only_llm(key: str) -> Plan:
    return Plan(
        turn_classification=_ok_tc(),
        clarity="reply_only",
        actions={},
        compose=ComposerCall(kind="llm", llm_prompt_key=key),
    )


def _reply_only_template(tid: str) -> Plan:
    return Plan(
        turn_classification=_ok_tc(),
        clarity="reply_only",
        actions={},
        compose=ComposerCall(kind="template", template_id=tid),
    )


# ---------------------------------------------------------------------------
# reply_only — valid paths
# ---------------------------------------------------------------------------


def test_reply_only_smalltalk_llm_accepted() -> None:
    """clarity='reply_only' + kind='llm' + smalltalk key → valid."""
    plan = _reply_only_llm("smalltalk")
    assert plan.clarity == "reply_only"
    assert plan.actions == {}
    assert plan.compose.llm_prompt_key == "smalltalk"


def test_reply_only_identity_llm_rejected() -> None:
    """clarity='reply_only' + kind='llm' + identity key → REJECTED.

    FIX 4 (rot-enablement Phase 1 R1): the identity LLM path is removed from
    CONVERSATIONAL_LLM_PROMPT_KEYS. The deterministic identity_playful template
    is the canonical identity reply (cheaper, reliable, guaranteed no model-name
    leak). Planners must route identity questions to identity_playful template.
    """
    with pytest.raises(ValidationError) as exc:
        _reply_only_llm("identity")
    assert "reply_only" in str(exc.value)
    assert "identity" in str(exc.value)


def test_reply_only_identity_playful_template_accepted() -> None:
    """clarity='reply_only' + kind='template' + identity_playful → valid."""
    plan = _reply_only_template("identity_playful")
    assert plan.clarity == "reply_only"
    assert plan.compose.template_id == "identity_playful"


# ---------------------------------------------------------------------------
# reply_only — invalid compose targets
# ---------------------------------------------------------------------------


def test_reply_only_non_conversational_llm_key_rejected() -> None:
    """Non-conversational LLM prompt (requires tool results) must be rejected
    for reply_only — it implies action results exist, which they don't."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={},
            compose=ComposerCall(kind="llm", llm_prompt_key="recipe_narrative"),
        )
    msg = str(exc.value)
    assert "reply_only" in msg
    assert "recipe_narrative" in msg or "llm_prompt_key" in msg


def test_reply_only_multi_action_summary_key_rejected() -> None:
    """multi_action_summary is explicitly for action results — not valid
    for reply_only (no actions to summarize)."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={},
            compose=ComposerCall(kind="llm", llm_prompt_key="multi_action_summary"),
        )
    assert "reply_only" in str(exc.value)


def test_reply_only_cooking_explanation_key_rejected() -> None:
    """cooking_explanation requires facts — not valid for reply_only."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={},
            compose=ComposerCall(kind="llm", llm_prompt_key="cooking_explanation"),
        )
    assert "reply_only" in str(exc.value)


def test_reply_only_humanize_result_key_rejected() -> None:
    """humanize_result requires tool results (intent+actions) — not valid
    for reply_only (no actions ran). Using it here would be a semantic
    mistake; the schema rejects it at the compose-key level."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={},
            compose=ComposerCall(kind="llm", llm_prompt_key="humanize_result"),
        )
    assert "reply_only" in str(exc.value)


def test_reply_only_non_conversational_template_rejected() -> None:
    """shopping_added_ok is a success template for tool results, not a
    conversational reply — must be rejected for reply_only."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={},
            compose=ComposerCall(kind="template", template_id="shopping_added_ok"),
        )
    msg = str(exc.value)
    assert "reply_only" in msg


def test_reply_only_clarification_template_rejected() -> None:
    """Clarification templates are for needs_clarification, not reply_only."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={},
            compose=ComposerCall(
                kind="template", template_id="ask_user_for_clarification"
            ),
        )
    assert "reply_only" in str(exc.value)


def test_reply_only_generic_tool_error_template_rejected() -> None:
    """Error templates are not conversational compose targets."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={},
            compose=ComposerCall(kind="template", template_id="generic_tool_error"),
        )
    assert "reply_only" in str(exc.value)


# ---------------------------------------------------------------------------
# reply_only — must have 0 actions (silent no-op guard)
# ---------------------------------------------------------------------------


def test_reply_only_with_actions_rejected() -> None:
    """reply_only + >=1 action = silent no-op: the planner dispatched a tool
    but routed to a conversational reply that ignores the result. Schema must
    reject this shape — it's a semantic error at schema level."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={"s1": _ok_action()},
            compose=ComposerCall(kind="llm", llm_prompt_key="smalltalk"),
        )
    msg = str(exc.value)
    assert "reply_only" in msg
    assert "action" in msg.lower()


def test_reply_only_with_multiple_actions_rejected() -> None:
    """Same guard — 2 actions also rejected."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={"s1": _ok_action(), "s2": _ok_action("list_reminders")},
            compose=ComposerCall(kind="llm", llm_prompt_key="smalltalk"),
        )
    assert "reply_only" in str(exc.value)


# ---------------------------------------------------------------------------
# Regression: clarity='clear' still requires >=1 action (unchanged)
# ---------------------------------------------------------------------------


def test_clear_with_empty_actions_still_rejected() -> None:
    """clarity='clear' + empty actions was already rejected before reply_only
    was added — must remain rejected (no regression)."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="clear",
            actions={},
            compose=ComposerCall(kind="template", template_id="shopping_added_ok"),
        )
    assert "at least one action" in str(exc.value).lower()


def test_clear_with_actions_still_accepted() -> None:
    """Sanity: the normal action plan path is unaffected."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="clear",
        actions={"s1": _ok_action()},
        compose=ComposerCall(kind="template", template_id="shopping_added_ok"),
    )
    assert plan.clarity == "clear"


# ---------------------------------------------------------------------------
# Semantic misclassification is a planner-prompt concern (Phase 3)
# ---------------------------------------------------------------------------


def test_semantic_misclassification_is_planner_prompt_concern() -> None:
    """The schema can only enforce SHAPE (reply_only + 0 actions + conversational
    compose). Semantic misclassification — e.g. a planner that routes
    "добавь молоко" as reply_only instead of clear+actions — is a planner
    LLM prompt quality problem, not a schema problem.

    This test documents that limitation: the schema accepts a grammatically
    valid reply_only plan even if the intent string happens to look like an
    action request. The schema has no NLU; fixing planner mis-routing is
    Phase 3 (planner prompt improvement).
    """
    # Grammatically valid: reply_only + 0 actions + smalltalk compose.
    # Semantically odd (user may have meant an action) — not detectable here.
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="reply_only",
        actions={},
        compose=ComposerCall(kind="llm", llm_prompt_key="smalltalk"),
    )
    # Schema accepts it — planner prompt is responsible for not emitting this
    # shape for action requests.
    assert plan.clarity == "reply_only"


# ---------------------------------------------------------------------------
# CONVERSATIONAL constants shape
# ---------------------------------------------------------------------------


def test_conversational_llm_prompt_keys_is_frozenset() -> None:
    assert isinstance(CONVERSATIONAL_LLM_PROMPT_KEYS, frozenset)
    assert "smalltalk" in CONVERSATIONAL_LLM_PROMPT_KEYS
    # FIX 4: identity LLM key removed from CONVERSATIONAL_LLM_PROMPT_KEYS.
    # identity_playful template (CONVERSATIONAL_TEMPLATE_IDS) is the canonical path.
    assert "identity" not in CONVERSATIONAL_LLM_PROMPT_KEYS


def test_conversational_template_ids_is_frozenset() -> None:
    assert isinstance(CONVERSATIONAL_TEMPLATE_IDS, frozenset)
    assert "identity_playful" in CONVERSATIONAL_TEMPLATE_IDS


def test_conversational_llm_keys_do_not_overlap_with_non_conversational() -> None:
    """Conversational keys must not include any result-narrative key
    that requires tool data — mixing them would undermine the guard."""
    non_conversational = {
        "recipe_narrative",
        "recipe_added_to_shopping_narrative",
        "multi_action_summary",
        "cooking_explanation",
        "humanize_result",
    }
    assert CONVERSATIONAL_LLM_PROMPT_KEYS.isdisjoint(non_conversational), (
        f"Conversational keys overlap with non-conversational: "
        f"{CONVERSATIONAL_LLM_PROMPT_KEYS & non_conversational}"
    )


# ---------------------------------------------------------------------------
# Identity template — denylist + non-empty
# ---------------------------------------------------------------------------


_MODEL_DENYLIST_PATTERN = re.compile(
    r"\b(mimo|openai|gpt|qwen|claude|anthropic)\b",
    re.IGNORECASE,
)


def test_identity_template_non_empty() -> None:
    """The identity template must produce a non-empty reply."""
    from sreda.services.composer.templates_housewife import HOUSEWIFE_TEMPLATES

    tmpl = HOUSEWIFE_TEMPLATES["identity_playful"]
    assert tmpl.strip(), "identity_playful template must not be empty"


def test_identity_template_denylist_no_real_model_names() -> None:
    """The identity template must NEVER contain real model/provider names.
    Denylist: mimo, openai, gpt, qwen, claude, anthropic (case-insensitive).
    Leaking the real model name breaks the «играючи, не раскрывает»
    requirement (R2 decision)."""
    from sreda.services.composer.templates_housewife import HOUSEWIFE_TEMPLATES

    tmpl = HOUSEWIFE_TEMPLATES["identity_playful"]
    match = _MODEL_DENYLIST_PATTERN.search(tmpl)
    assert match is None, (
        f"identity_playful template contains a real model/provider name: "
        f"{match.group()!r} — must not reveal the underlying model. "
        f"Use the Среда persona, not the actual provider."
    )


def test_identity_template_registers_in_registry() -> None:
    """identity_playful must be in the global REGISTRY so the composer
    can render it (CONVERSATIONAL_TEMPLATE_IDS allowlist references it)."""
    from sreda.services.composer import REGISTRY

    assert "identity_playful" in REGISTRY.template_ids(), (
        "identity_playful not registered in REGISTRY — add it to "
        "HOUSEWIFE_TEMPLATES in templates_housewife.py"
    )


def test_identity_template_renders_as_среда() -> None:
    """The template must mention 'Среда' as the assistant's name."""
    from sreda.services.composer.templates_housewife import HOUSEWIFE_TEMPLATES

    tmpl = HOUSEWIFE_TEMPLATES["identity_playful"]
    assert "Среда" in tmpl, (
        "identity_playful must introduce the assistant as 'Среда'"
    )


# ---------------------------------------------------------------------------
# LLM prompt specs — validate_data for smalltalk and humanize_result
# ---------------------------------------------------------------------------


def test_smalltalk_validate_data_accepts_valid_payload() -> None:
    """smalltalk requires user_message — must accept when present."""
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY

    LLM_PROMPT_REGISTRY.validate_data(
        "smalltalk",
        {"user_message": "привет!", "profile_name": "Борис"},
    )  # no raise


def test_smalltalk_validate_data_accepts_minimal_payload() -> None:
    """Only user_message is required — profile_name is optional."""
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY

    LLM_PROMPT_REGISTRY.validate_data(
        "smalltalk",
        {"user_message": "как дела?"},
    )  # no raise


def test_smalltalk_validate_data_rejects_missing_user_message() -> None:
    """Missing user_message → ComposerInputError (fail-fast before LLM call)."""
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="user_message"):
        LLM_PROMPT_REGISTRY.validate_data("smalltalk", {})


def test_smalltalk_validate_data_rejects_blank_user_message() -> None:
    """Blank user_message is treated as missing."""
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="user_message"):
        LLM_PROMPT_REGISTRY.validate_data("smalltalk", {"user_message": "   "})


def test_humanize_result_validate_data_accepts_valid_payload() -> None:
    """humanize_result strict contract: {intent, actions} only at top level;
    each action item exactly {user_visible_summary, status} — must accept."""
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY

    LLM_PROMPT_REGISTRY.validate_data(
        "humanize_result",
        {
            "intent": "добавь молоко и хлеб в покупки",
            "actions": [
                {
                    "user_visible_summary": "молоко и хлеб добавлены в список покупок",
                    "status": "added",
                },
            ],
        },
    )  # no raise


def test_humanize_result_validate_data_rejects_missing_intent() -> None:
    """Without intent the model lacks request context."""
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="intent"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {"actions": [{"user_visible_summary": "ok", "status": "ok"}]},
        )


def test_humanize_result_validate_data_rejects_missing_actions() -> None:
    """Without actions the model would fabricate results."""
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="actions"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {"intent": "добавь молоко"},
        )


def test_humanize_result_validate_data_rejects_empty_actions_list() -> None:
    """Empty list for actions is treated as blank (no results to narrate)."""
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="actions"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {"intent": "добавь молоко", "actions": []},
        )


def test_humanize_result_validate_data_rejects_blank_intent() -> None:
    """Whitespace-only intent is blank."""
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="intent"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {
                "intent": "   ",
                "actions": [{"user_visible_summary": "ok", "status": "ok"}],
            },
        )


# ---------------------------------------------------------------------------
# Registry sync: HOUSEWIFE_LLM_PROMPTS keys include the new conversational ones
# ---------------------------------------------------------------------------


def test_housewife_llm_prompts_contains_smalltalk() -> None:
    from sreda.services.composer.llm_prompts_housewife import HOUSEWIFE_LLM_PROMPTS

    assert "smalltalk" in HOUSEWIFE_LLM_PROMPTS


def test_housewife_llm_prompts_contains_humanize_result() -> None:
    from sreda.services.composer.llm_prompts_housewife import HOUSEWIFE_LLM_PROMPTS

    assert "humanize_result" in HOUSEWIFE_LLM_PROMPTS


def test_housewife_llm_prompts_still_contains_existing_keys() -> None:
    """Regression: old keys must not be removed."""
    from sreda.services.composer.llm_prompts_housewife import HOUSEWIFE_LLM_PROMPTS

    for key in (
        "recipe_narrative",
        "recipe_added_to_shopping_narrative",
        "multi_action_summary",
        "cooking_explanation",
    ):
        assert key in HOUSEWIFE_LLM_PROMPTS, f"existing key {key!r} missing"


def test_default_registry_contains_smalltalk_and_humanize_result() -> None:
    """LLM_PROMPT_REGISTRY is built from HOUSEWIFE_LLM_PROMPTS — new keys
    must be visible in the singleton used by the composer."""
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY

    assert "smalltalk" in LLM_PROMPT_REGISTRY.prompt_keys()
    assert "humanize_result" in LLM_PROMPT_REGISTRY.prompt_keys()


def test_conversational_llm_prompt_keys_registered_in_llm_registry() -> None:
    """Every key in CONVERSATIONAL_LLM_PROMPT_KEYS must be in LLM_PROMPT_REGISTRY.
    If a key is added to the allowlist but not registered, the composer would
    raise UnknownLLMPromptError at runtime — catch at CI time instead."""
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY

    registered = set(LLM_PROMPT_REGISTRY.prompt_keys())
    for key in CONVERSATIONAL_LLM_PROMPT_KEYS:
        assert key in registered, (
            f"CONVERSATIONAL_LLM_PROMPT_KEYS contains {key!r} but it is not "
            f"registered in LLM_PROMPT_REGISTRY. Add it to "
            f"HOUSEWIFE_LLM_PROMPTS in llm_prompts_housewife.py."
        )


def test_conversational_template_ids_registered_in_template_registry() -> None:
    """Every template id in CONVERSATIONAL_TEMPLATE_IDS must be in REGISTRY.
    Same deploy-race guard as clarification_template_ids_match_schema_allowlist."""
    from sreda.services.composer import REGISTRY

    registered = set(REGISTRY.template_ids())
    for tid in CONVERSATIONAL_TEMPLATE_IDS:
        assert tid in registered, (
            f"CONVERSATIONAL_TEMPLATE_IDS contains {tid!r} but it is not "
            f"registered in REGISTRY. Add it to HOUSEWIFE_TEMPLATES in "
            f"templates_housewife.py."
        )


def test_smalltalk_spec_has_description() -> None:
    from sreda.services.composer.llm_prompts_housewife import HOUSEWIFE_LLM_PROMPTS

    assert HOUSEWIFE_LLM_PROMPTS["smalltalk"].description


def test_humanize_result_spec_has_description() -> None:
    from sreda.services.composer.llm_prompts_housewife import HOUSEWIFE_LLM_PROMPTS

    assert HOUSEWIFE_LLM_PROMPTS["humanize_result"].description


# ---------------------------------------------------------------------------
# FIX 1 — inverse mis-routing loophole
# ---------------------------------------------------------------------------


def test_clear_plan_with_smalltalk_compose_rejected() -> None:
    """clarity='clear' + plan-level compose pointing at 'smalltalk' → rejected.

    Would run actions then discard results with a conversational reply.
    FIX 1 (Phase 1 R1 MAJOR).
    """
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="clear",
            actions={"s1": _ok_action()},
            compose=ComposerCall(kind="llm", llm_prompt_key="smalltalk"),
        )
    assert "conversational" in str(exc.value).lower() or "smalltalk" in str(exc.value)


def test_clear_plan_with_identity_playful_compose_rejected() -> None:
    """clarity='clear' + plan-level compose pointing at 'identity_playful' → rejected.

    identity_playful is a conversational template only valid for reply_only.
    FIX 1 (Phase 1 R1 MAJOR).
    """
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="clear",
            actions={"s1": _ok_action()},
            compose=ComposerCall(kind="template", template_id="identity_playful"),
        )
    assert "conversational" in str(exc.value).lower() or "identity_playful" in str(exc.value)


def test_branch_compose_identity_playful_rejected() -> None:
    """Branch-level compose pointing at identity_playful → rejected regardless of clarity.

    Branch composes fire after tool execution; using a conversational target there
    would discard tool results. FIX 1 (Phase 1 R1 MAJOR).
    """
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="clear",
            actions={
                "s1": Action(
                    tool="list_shopping",
                    args={},
                    expected_outcomes=[
                        OutcomeBranch(
                            match={"status": "ok"},
                            compose=ComposerCall(
                                kind="template", template_id="identity_playful"
                            ),
                        )
                    ],
                )
            },
            compose=ComposerCall(kind="template", template_id="shopping_list_show"),
        )
    assert "conversational" in str(exc.value).lower() or "identity_playful" in str(exc.value)


def test_branch_compose_smalltalk_rejected() -> None:
    """Branch-level compose pointing at 'smalltalk' → rejected.

    FIX 1 (Phase 1 R1 MAJOR) — same inverse mis-routing guard.
    """
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="clear",
            actions={
                "s1": Action(
                    tool="list_shopping",
                    args={},
                    expected_outcomes=[
                        OutcomeBranch(
                            match={"status": "ok"},
                            compose=ComposerCall(
                                kind="llm", llm_prompt_key="smalltalk"
                            ),
                        )
                    ],
                )
            },
            compose=ComposerCall(kind="template", template_id="shopping_list_show"),
        )
    assert "conversational" in str(exc.value).lower() or "smalltalk" in str(exc.value)


def test_reply_only_smalltalk_still_valid_after_fix1() -> None:
    """reply_only + smalltalk plan-level compose must still be accepted.

    FIX 1 only blocks conversational targets in non-reply_only clarity.
    """
    plan = _reply_only_llm("smalltalk")
    assert plan.clarity == "reply_only"
    assert plan.compose.llm_prompt_key == "smalltalk"


def test_needs_clarification_with_smalltalk_compose_rejected() -> None:
    """clarity='needs_clarification' + smalltalk compose → rejected.

    Conversational targets are banned in all non-reply_only modes.
    FIX 1 (Phase 1 R1 MAJOR).
    """
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="needs_clarification",
            clarity_reason="не знаю что делать",
            actions={},
            compose=ComposerCall(kind="llm", llm_prompt_key="smalltalk"),
        )
    # Should fail — either on the conversational-compose guard or on the
    # needs_clarification compose.kind check (which requires kind='template').
    assert exc.value is not None


# ---------------------------------------------------------------------------
# FIX 2 — smalltalk nondisclosure in prompt text
# ---------------------------------------------------------------------------


def test_smalltalk_system_prompt_contains_nondisclosure() -> None:
    """_SMALLTALK.system_prompt must contain the nondisclosure instruction.

    Previously it was only a Python comment (invisible at runtime).
    FIX 2 (Phase 1 R1 MAJOR) moves it into the actual string.
    """
    from sreda.services.composer.llm_prompts_housewife import HOUSEWIFE_LLM_PROMPTS

    prompt = HOUSEWIFE_LLM_PROMPTS["smalltalk"].system_prompt
    # The instruction must be an ACTIVE part of the prompt, not a comment.
    # Check for the key nondisclosure terms in the actual string.
    assert "MiMo" in prompt or "mimo" in prompt.lower(), (
        "smalltalk.system_prompt must mention MiMo in the nondisclosure rule"
    )
    assert "OpenAI" in prompt or "openai" in prompt.lower(), (
        "smalltalk.system_prompt must mention OpenAI in the nondisclosure rule"
    )
    assert "не называй" in prompt or "не упоминай" in prompt or "не раскрывай" in prompt, (
        "smalltalk.system_prompt must contain a nondisclosure instruction"
    )


def test_smalltalk_nondisclosure_not_only_comment() -> None:
    """The nondisclosure instruction must be a string token, not just # comment.

    This is the specific regression the fix addresses — the instruction was
    inside a Python comment and thus absent from the actual system_prompt.
    """
    from sreda.services.composer.llm_prompts_housewife import HOUSEWIFE_LLM_PROMPTS

    prompt = HOUSEWIFE_LLM_PROMPTS["smalltalk"].system_prompt
    # The key phrase from FIX 2
    assert "ВАЖНО" in prompt or "важно" in prompt.lower(), (
        "smalltalk.system_prompt must contain the nondisclosure marker (ВАЖНО)"
    )


# ---------------------------------------------------------------------------
# FIX 3 — humanize_result strict typed data contract
# ---------------------------------------------------------------------------


def test_humanize_result_rejects_extra_top_level_key() -> None:
    """Extra top-level keys beyond {intent, actions} must be rejected.

    Prevents PII / internal fields from reaching the LLM.
    FIX 3 (Phase 1 R1 MAJOR).
    """
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="disallowed"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {
                "intent": "добавь молоко",
                "actions": [{"user_visible_summary": "добавила", "status": "ok"}],
                "execution_id": "exec-abc123",  # internal field — must be rejected
            },
        )


def test_humanize_result_rejects_action_with_raw_tool_field() -> None:
    """Action item with 'tool' field must be rejected.

    Raw tool names are internal identifiers. FIX 3 (Phase 1 R1 MAJOR).
    """
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="disallowed"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {
                "intent": "добавь молоко",
                "actions": [
                    {
                        "tool": "add_shopping_items",
                        "user_visible_summary": "молоко добавлено",
                        "status": "added",
                    }
                ],
            },
        )


def test_humanize_result_rejects_action_with_execution_id_field() -> None:
    """Action item with 'execution_id' field must be rejected.

    FIX 3 (Phase 1 R1 MAJOR).
    """
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="disallowed"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {
                "intent": "добавь молоко",
                "actions": [
                    {
                        "user_visible_summary": "молоко добавлено",
                        "status": "added",
                        "execution_id": "exec-xyz",
                    }
                ],
            },
        )


def test_humanize_result_rejects_action_with_raw_error_field() -> None:
    """Action item with 'error' field must be rejected.

    Raw error strings are internal. FIX 3 (Phase 1 R1 MAJOR).
    """
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="disallowed"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {
                "intent": "добавь молоко",
                "actions": [
                    {
                        "user_visible_summary": "не удалось",
                        "status": "error",
                        "error": "ItemAlreadyExists: молоко",
                    }
                ],
            },
        )


def test_humanize_result_accepts_well_formed_payload() -> None:
    """Well-formed {user_visible_summary, status} action items must be accepted.

    FIX 3 (Phase 1 R1 MAJOR) — confirms the happy path with strict contract.
    """
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY

    LLM_PROMPT_REGISTRY.validate_data(
        "humanize_result",
        {
            "intent": "добавь молоко и хлеб в покупки",
            "actions": [
                {"user_visible_summary": "молоко добавлено в список покупок", "status": "added"},
                {"user_visible_summary": "хлеб добавлен в список покупок", "status": "added"},
            ],
        },
    )  # no raise


def test_humanize_result_rejects_empty_user_visible_summary() -> None:
    """Blank user_visible_summary in an action item must be rejected.

    FIX 3 (Phase 1 R1 MAJOR).
    """
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="user_visible_summary"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {
                "intent": "добавь молоко",
                "actions": [{"user_visible_summary": "   ", "status": "added"}],
            },
        )


def test_humanize_result_rejects_missing_status_in_action() -> None:
    """Action item without 'status' must be rejected.

    FIX 3 (Phase 1 R1 MAJOR).
    """
    from sreda.services.composer.prompts_registry import (
        ComposerInputError,
        LLM_PROMPT_REGISTRY,
    )

    with pytest.raises(ComposerInputError, match="status"):
        LLM_PROMPT_REGISTRY.validate_data(
            "humanize_result",
            {
                "intent": "добавь молоко",
                "actions": [{"user_visible_summary": "молоко добавлено"}],
            },
        )


# ---------------------------------------------------------------------------
# FIX 4 — identity LLM key removed from reply_only allowlist
# ---------------------------------------------------------------------------


def test_identity_llm_key_not_in_conversational_keys() -> None:
    """CONVERSATIONAL_LLM_PROMPT_KEYS must NOT contain 'identity'.

    FIX 4 (Phase 1 R1 MINOR-accepted): identity_playful template is the
    canonical path; the LLM identity path is a footgun.
    """
    assert "identity" not in CONVERSATIONAL_LLM_PROMPT_KEYS


def test_identity_llm_key_not_registered_in_housewife_prompts() -> None:
    """'identity' LLM key must not be in HOUSEWIFE_LLM_PROMPTS registry dict.

    The _IDENTITY spec may exist as an unregistered object, but must NOT
    be in the public dict that feeds the planner allowlist.
    FIX 4 (Phase 1 R1).
    """
    from sreda.services.composer.llm_prompts_housewife import HOUSEWIFE_LLM_PROMPTS

    assert "identity" not in HOUSEWIFE_LLM_PROMPTS, (
        "'identity' LLM key must not be registered in HOUSEWIFE_LLM_PROMPTS. "
        "Use identity_playful template (CONVERSATIONAL_TEMPLATE_IDS) instead."
    )


# ---------------------------------------------------------------------------
# FIX 5 — strengthened identity denylist test
# ---------------------------------------------------------------------------

# Approved exact text for identity_playful — snapshot to catch future drift.
_IDENTITY_PLAYFUL_APPROVED_TEXT = (
    "Я Среда — твоя помощница по дому и делам 🏠✨ "
    "Слежу за списком покупок, напоминаниями, рецептами и всем, "
    "что нужно по хозяйству.\n\n"
    "А что у меня «под капотом» — маленький секрет 😄 "
    "Скажем так: магия и немного технологий."
)

# Expanded denylist: ASCII, Cyrillic, translit, spaced variants.
# Normalized (lowercased, stripped) before matching.
_EXPANDED_DENYLIST = [
    "mimo",
    "openai",
    "open ai",
    "gpt",
    "chatgpt",
    "gpt4",
    "gpt-4",
    "qwen",
    "claude",
    "anthropic",
    # Cyrillic / translit variants
    "джипити",
    "чатжпт",
    "клод",
    "мимо",
    "опенай",
    "антропик",
]


def _normalize(text: str) -> str:
    """Lowercase and collapse multiple spaces for denylist matching."""
    import unicodedata
    text = unicodedata.normalize("NFKC", text).lower()
    # collapse punctuation runs to single space for "open ai" / "gpt-4" matching
    return re.sub(r"[\s\-_]+", " ", text).strip()


def test_identity_template_snapshot() -> None:
    """identity_playful template must exactly match the approved snapshot.

    Any text change breaks this test — forcing a conscious review before
    updating the snapshot. FIX 5 (Phase 1 R1 MINOR).
    """
    from sreda.services.composer.templates_housewife import HOUSEWIFE_TEMPLATES

    tmpl = HOUSEWIFE_TEMPLATES["identity_playful"]
    assert tmpl == _IDENTITY_PLAYFUL_APPROVED_TEXT, (
        "identity_playful template text has changed from the approved snapshot. "
        "If the change is intentional, update _IDENTITY_PLAYFUL_APPROVED_TEXT "
        "in this test file AND verify the denylist still passes."
    )


def test_identity_template_expanded_denylist() -> None:
    """Normalized identity_playful text must not contain any denylist term.

    Covers ASCII, Cyrillic/translit, spaced, and hyphenated variants.
    FIX 5 (Phase 1 R1 MINOR).
    """
    from sreda.services.composer.templates_housewife import HOUSEWIFE_TEMPLATES

    tmpl = HOUSEWIFE_TEMPLATES["identity_playful"]
    normalized = _normalize(tmpl)
    for term in _EXPANDED_DENYLIST:
        assert term not in normalized, (
            f"identity_playful template contains denylist term {term!r} "
            f"(after normalization). Must not reveal real model/provider names."
        )


# ---------------------------------------------------------------------------
# Phase-2 R1 MINOR-2 — boundary eval fixtures (pure action / mixed / identity)
# These assert the SHAPE the planner SHOULD emit for the three boundary
# utterance classes Codex flagged, complementing the rejection tests above
# (silent no-op + branch conversational compose). Schema has no NLU, so this
# documents the target shapes; planner-prompt quality is what routes there.
# ---------------------------------------------------------------------------


def _action_with_result_compose() -> Action:
    """An action whose terminal branch uses a result-aware template (the
    correct shape — NEVER smalltalk/identity_playful in a branch)."""
    return Action(
        tool="add_shopping_items",
        args={"items": [{"title": "молоко"}]},
        expected_outcomes=[
            OutcomeBranch(
                match={"status": "added"},
                compose=ComposerCall(
                    kind="template",
                    template_id="shopping_added_ok",
                    template_data={"items": ["молоко"]},
                ),
            )
        ],
    )


def test_pure_action_is_clear_with_result_template() -> None:
    """«добавь молоко» → clear + action + result-aware compose. Valid shape."""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="clear",
        actions={"s1": _action_with_result_compose()},
        compose=ComposerCall(
            kind="template",
            template_id="shopping_added_ok",
            template_data={"items": ["молоко"]},
        ),
    )
    assert plan.clarity == "clear"
    assert plan.actions  # >= 1 action
    # No conversational compose anywhere.
    assert plan.compose.template_id == "shopping_added_ok"


def test_mixed_greeting_plus_action_is_clear_not_reply_only() -> None:
    """«привет! добавь молоко» — the greeting must be ignored: the plan is
    clear with the action, NOT reply_only. (Models few-shot ex10.)"""
    plan = Plan(
        turn_classification=_ok_tc(),
        clarity="clear",
        actions={"s1": _action_with_result_compose()},
        compose=ComposerCall(
            kind="template",
            template_id="shopping_added_ok",
            template_data={"items": ["молоко"]},
        ),
    )
    assert plan.clarity == "clear"
    assert plan.compose.llm_prompt_key is None  # not a conversational reply


def test_mixed_greeting_plus_action_as_reply_only_is_rejected() -> None:
    """The WRONG shape for «привет! добавь молоко» — reply_only+smalltalk with
    an action — is the silent no-op the schema rejects (the result would be
    discarded). Confirms the boundary is enforced, not just documented."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="reply_only",
            actions={"s1": _action_with_result_compose()},
            compose=ComposerCall(kind="llm", llm_prompt_key="smalltalk"),
        )
    msg = str(exc.value)
    assert "reply_only" in msg and "action" in msg.lower()


def test_identity_question_is_reply_only_template_zero_actions() -> None:
    """«кто ты?» → reply_only + identity_playful template + 0 actions."""
    plan = _reply_only_template("identity_playful")
    assert plan.clarity == "reply_only"
    assert plan.actions == {}
    assert plan.compose.template_id == "identity_playful"


# ---------------------------------------------------------------------------
# Phase-2 R2 MAJOR — gate-OFF greeting shape (Codex high R2).
# When the smalltalk LLM key is disabled, a pure greeting must still have a
# clean valid reply_only shape: kind='template' + smalltalk_fallback. This
# is the deterministic warm greeting (no LLM), so greetings never degrade to
# an invalid plan or an awkward clarification when rot is off.
# ---------------------------------------------------------------------------


def test_smalltalk_fallback_in_conversational_template_ids() -> None:
    """smalltalk_fallback must be an allowed reply_only template target."""
    assert "smalltalk_fallback" in CONVERSATIONAL_TEMPLATE_IDS


def test_reply_only_smalltalk_fallback_template_accepted() -> None:
    """clarity='reply_only' + kind='template' + smalltalk_fallback → valid
    (gate-off greeting path)."""
    plan = _reply_only_template("smalltalk_fallback")
    assert plan.clarity == "reply_only"
    assert plan.actions == {}
    assert plan.compose.template_id == "smalltalk_fallback"


def test_smalltalk_fallback_registered_in_registry() -> None:
    """smalltalk_fallback must be a real template the composer can render."""
    from sreda.services.composer import REGISTRY

    assert "smalltalk_fallback" in REGISTRY.template_ids()


def test_clear_plan_with_smalltalk_fallback_compose_rejected() -> None:
    """smalltalk_fallback is conversational — banned in a clear action plan
    (would discard tool results), same guard as smalltalk/identity_playful."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="clear",
            actions={"s1": _ok_action()},
            compose=ComposerCall(kind="template", template_id="smalltalk_fallback"),
        )
    msg = str(exc.value)
    assert "conversational" in msg.lower() or "smalltalk_fallback" in msg


def test_branch_compose_smalltalk_fallback_rejected() -> None:
    """Branch-level compose pointing at smalltalk_fallback → rejected."""
    with pytest.raises(ValidationError) as exc:
        Plan(
            turn_classification=_ok_tc(),
            clarity="clear",
            actions={
                "s1": Action(
                    tool="list_shopping",
                    args={},
                    expected_outcomes=[
                        OutcomeBranch(
                            match={"status": "ok"},
                            compose=ComposerCall(
                                kind="template", template_id="smalltalk_fallback"
                            ),
                        )
                    ],
                )
            },
            compose=ComposerCall(kind="template", template_id="shopping_list_show"),
        )
    msg = str(exc.value)
    assert "conversational" in msg.lower() or "smalltalk_fallback" in msg
