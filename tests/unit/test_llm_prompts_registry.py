"""Tests for services/composer/prompts_registry.py — Sub-A12 Phase D.2.

Covers:
- register / get / UnknownLLMPromptError
- validate_data: present / missing / blank required keys → ComposerInputError
- prompt_keys / descriptions
- snapshot_hash stability + change-detection
- default registry covers expected housewife keys
- every registered spec is well-formed (anti-fabrication guard present)
"""

from __future__ import annotations

import pytest

from sreda.services.composer.llm_prompts_housewife import (
    HOUSEWIFE_LLM_PROMPTS,
    LLMPromptSpec,
)
from sreda.services.composer.prompts_registry import (
    LLM_PROMPT_REGISTRY,
    ComposerInputError,
    LLMPromptRegistry,
    UnknownLLMPromptError,
)


# ---------------------------------------------------------------------------
# register / get
# ---------------------------------------------------------------------------


def test_register_and_get() -> None:
    reg = LLMPromptRegistry()
    spec = LLMPromptSpec(system_prompt="hi", required_keys=frozenset({"x"}))
    reg.register("k", spec)
    assert reg.get("k") is spec


def test_get_unknown_raises() -> None:
    reg = LLMPromptRegistry()
    with pytest.raises(UnknownLLMPromptError):
        reg.get("nope")


def test_register_empty_key_rejected() -> None:
    reg = LLMPromptRegistry()
    with pytest.raises(ValueError, match="non-empty"):
        reg.register("", LLMPromptSpec(system_prompt="x"))


def test_register_empty_system_prompt_rejected() -> None:
    reg = LLMPromptRegistry()
    with pytest.raises(ValueError, match="empty system_prompt"):
        reg.register("k", LLMPromptSpec(system_prompt=""))


def test_re_register_overwrites() -> None:
    reg = LLMPromptRegistry()
    reg.register("k", LLMPromptSpec(system_prompt="a"))
    reg.register("k", LLMPromptSpec(system_prompt="b"))
    assert reg.get("k").system_prompt == "b"


# ---------------------------------------------------------------------------
# validate_data
# ---------------------------------------------------------------------------


def _reg_with(required: set[str]) -> LLMPromptRegistry:
    reg = LLMPromptRegistry()
    reg.register("k", LLMPromptSpec(
        system_prompt="x", required_keys=frozenset(required),
    ))
    return reg


def test_validate_data_all_present_ok() -> None:
    reg = _reg_with({"a", "b"})
    reg.validate_data("k", {"a": "1", "b": [1, 2]})  # no raise


def test_validate_data_missing_key_raises() -> None:
    reg = _reg_with({"a", "b"})
    with pytest.raises(ComposerInputError, match="b"):
        reg.validate_data("k", {"a": "1"})


def test_validate_data_blank_values_treated_as_missing() -> None:
    reg = _reg_with({"a", "b", "c", "d"})
    with pytest.raises(ComposerInputError) as exc:
        reg.validate_data("k", {"a": None, "b": "", "c": [], "d": {}})
    msg = str(exc.value)
    for k in ("a", "b", "c", "d"):
        assert k in msg


def test_validate_data_unknown_key_raises_unknown() -> None:
    reg = _reg_with({"a"})
    with pytest.raises(UnknownLLMPromptError):
        reg.validate_data("other", {"a": "1"})


def test_validate_data_no_required_keys_always_ok() -> None:
    reg = LLMPromptRegistry()
    reg.register("k", LLMPromptSpec(system_prompt="x"))  # no required
    reg.validate_data("k", {})  # no raise


def test_validate_data_zero_int_is_valid_not_blank() -> None:
    """0 is a legitimate value (e.g. added_count=0), NOT blank."""
    reg = _reg_with({"n"})
    reg.validate_data("k", {"n": 0})  # no raise — 0 is not blank


def test_validate_data_whitespace_only_string_is_blank() -> None:
    """Codex D.2 R1 MINOR A#5 — '   ' is as useless as '' → blank."""
    reg = _reg_with({"s"})
    with pytest.raises(ComposerInputError, match="s"):
        reg.validate_data("k", {"s": "   \n\t "})


def test_validate_data_false_bool_is_valid_not_blank() -> None:
    """False is a legitimate value, NOT blank (parity with 0)."""
    reg = _reg_with({"flag"})
    reg.validate_data("k", {"flag": False})  # no raise


# ---------------------------------------------------------------------------
# prompt_keys / descriptions
# ---------------------------------------------------------------------------


def test_prompt_keys_sorted() -> None:
    reg = LLMPromptRegistry()
    reg.register("zebra", LLMPromptSpec(system_prompt="x"))
    reg.register("alpha", LLMPromptSpec(system_prompt="x"))
    assert reg.prompt_keys() == ["alpha", "zebra"]


def test_descriptions_map() -> None:
    reg = LLMPromptRegistry()
    reg.register("k", LLMPromptSpec(system_prompt="x", description="does X"))
    assert reg.descriptions() == {"k": "does X"}


# ---------------------------------------------------------------------------
# snapshot_hash
# ---------------------------------------------------------------------------


def test_snapshot_hash_stable() -> None:
    reg1 = _reg_with({"a"})
    reg2 = _reg_with({"a"})
    assert reg1.snapshot_hash() == reg2.snapshot_hash()


def test_snapshot_hash_changes_on_system_prompt_change() -> None:
    reg1 = LLMPromptRegistry()
    reg1.register("k", LLMPromptSpec(system_prompt="a"))
    reg2 = LLMPromptRegistry()
    reg2.register("k", LLMPromptSpec(system_prompt="b"))
    assert reg1.snapshot_hash() != reg2.snapshot_hash()


def test_snapshot_hash_changes_on_required_keys_change() -> None:
    reg1 = LLMPromptRegistry()
    reg1.register("k", LLMPromptSpec(system_prompt="x", required_keys=frozenset({"a"})))
    reg2 = LLMPromptRegistry()
    reg2.register("k", LLMPromptSpec(system_prompt="x", required_keys=frozenset({"a", "b"})))
    assert reg1.snapshot_hash() != reg2.snapshot_hash()


def test_snapshot_hash_changes_on_key_added() -> None:
    reg1 = _reg_with({"a"})
    reg2 = _reg_with({"a"})
    reg2.register("extra", LLMPromptSpec(system_prompt="x"))
    assert reg1.snapshot_hash() != reg2.snapshot_hash()


# ---------------------------------------------------------------------------
# Default registry contents
# ---------------------------------------------------------------------------


def test_default_registry_covers_expected_housewife_keys() -> None:
    expected = {
        "recipe_narrative",
        "recipe_added_to_shopping_narrative",
        "multi_action_summary",
        "cooking_explanation",
        # rot-enablement Phase 1 (issue #88)
        # NOTE: "identity" LLM key intentionally NOT registered — the
        # deterministic identity_playful template is the canonical path
        # (FIX 4, Phase 1 R1).
        "smalltalk",
        "humanize_result",
    }
    assert set(LLM_PROMPT_REGISTRY.prompt_keys()) == expected


def test_default_registry_matches_housewife_prompts_dict() -> None:
    """Registry singleton is built from HOUSEWIFE_LLM_PROMPTS — keys agree."""
    assert set(LLM_PROMPT_REGISTRY.prompt_keys()) == set(HOUSEWIFE_LLM_PROMPTS)


def test_every_prompt_has_anti_fabrication_guard() -> None:
    """Every data-grounded prompt's system text MUST carry the «только факты
    из ДАННЫХ» guard — the LLM-path's one defense against додумывание.

    Exception: conversational prompts (``smalltalk``, ``identity``) have NO
    ДАННЫЕ block — they operate from persona alone. Requiring the guard
    there would be nonsensical («опирайся на факты из блока ДАННЫЕ» when
    ДАННЫЕ is absent). They are explicitly exempt; their hallucination
    surface is bounded by having no structured input to distort.
    """
    # Conversational prompts operate from persona alone — no ДАННЫЕ block.
    # The anti-fabrication guard is inapplicable and intentionally absent.
    # NOTE: "identity" LLM key is no longer registered (FIX 4, Phase 1 R1)
    # so it won't appear in HOUSEWIFE_LLM_PROMPTS iteration below.
    _CONVERSATIONAL_EXEMPT = frozenset({"smalltalk"})
    for key, spec in HOUSEWIFE_LLM_PROMPTS.items():
        if key in _CONVERSATIONAL_EXEMPT:
            continue
        low = spec.system_prompt.lower()
        assert "только на факты" in low, (
            f"prompt {key!r} missing anti-fabrication guard"
        )
        assert "ничего не придумывай" in low, (
            f"prompt {key!r} missing 'ничего не придумывай' instruction"
        )


# ---------------------------------------------------------------------------
# humanize_result runtime contract (validate_data delegates to the shared
# validate_humanize_result_payload with allow_refs=False — full enforcement)
# ---------------------------------------------------------------------------


def _hr_reg() -> LLMPromptRegistry:
    """Registry with only humanize_result registered (uses the real housewife
    spec so required_keys are intact)."""
    from sreda.services.composer.llm_prompts_housewife import HOUSEWIFE_LLM_PROMPTS
    reg = LLMPromptRegistry()
    reg.register("humanize_result", HOUSEWIFE_LLM_PROMPTS["humanize_result"])
    return reg


def test_hr_runtime_well_formed_passes() -> None:
    """Well-formed payload validates without error at runtime."""
    _hr_reg().validate_data("humanize_result", {
        "intent": "замовити продукти",
        "actions": [{"user_visible_summary": "Молоко додано", "status": "ok"}],
    })


def test_hr_runtime_missing_status_raises() -> None:
    """actions=[{user_visible_summary only}] → ComposerInputError at runtime."""
    with pytest.raises(ComposerInputError, match="status"):
        _hr_reg().validate_data("humanize_result", {
            "intent": "тест",
            "actions": [{"user_visible_summary": "Зроблено"}],
        })


def test_hr_runtime_extra_item_key_raises() -> None:
    """actions=[{user_visible_summary, status, tool}] → ComposerInputError."""
    with pytest.raises(ComposerInputError, match="tool"):
        _hr_reg().validate_data("humanize_result", {
            "intent": "тест",
            "actions": [{"user_visible_summary": "x", "status": "ok", "tool": "boom"}],
        })


def test_hr_runtime_non_dict_item_raises() -> None:
    """actions=[42] → ComposerInputError."""
    with pytest.raises(ComposerInputError):
        _hr_reg().validate_data("humanize_result", {
            "intent": "тест",
            "actions": [42],
        })


def test_hr_runtime_non_list_actions_raises() -> None:
    """actions='not-a-ref' → ComposerInputError (not a list)."""
    with pytest.raises(ComposerInputError, match="non-empty list"):
        _hr_reg().validate_data("humanize_result", {
            "intent": "тест",
            "actions": "not-a-ref",
        })


def test_hr_runtime_empty_list_actions_raises() -> None:
    """actions=[] → ComposerInputError (caught as blank required key or non-empty list)."""
    with pytest.raises(ComposerInputError):
        _hr_reg().validate_data("humanize_result", {
            "intent": "тест",
            "actions": [],
        })


def test_hr_runtime_blank_status_raises() -> None:
    """actions=[{user_visible_summary, status=''}] → ComposerInputError."""
    with pytest.raises(ComposerInputError, match="status"):
        _hr_reg().validate_data("humanize_result", {
            "intent": "тест",
            "actions": [{"user_visible_summary": "ok", "status": ""}],
        })


def test_hr_runtime_extra_top_key_raises() -> None:
    """Extra top-level key → ComposerInputError at runtime."""
    with pytest.raises(ComposerInputError, match="execution_id"):
        _hr_reg().validate_data("humanize_result", {
            "intent": "тест",
            "actions": [{"user_visible_summary": "x", "status": "ok"}],
            "execution_id": "abc",
        })


def test_cooking_explanation_requires_facts_not_just_question() -> None:
    """Codex D.2 R1 MAJOR A#1 — cooking_explanation must require BOTH
    question AND facts, else the model answers from priors."""
    spec = HOUSEWIFE_LLM_PROMPTS["cooking_explanation"]
    assert "question" in spec.required_keys
    assert "facts" in spec.required_keys
    # registry rejects a call with question but no facts
    with pytest.raises(ComposerInputError, match="facts"):
        LLM_PROMPT_REGISTRY.validate_data(
            "cooking_explanation", {"question": "сколько варить гречку?"},
        )


def test_every_prompt_has_required_keys_and_description() -> None:
    """Each prompt must declare required_keys (so validate_data can
    fail-fast) and a description (so the planner allowlist explains it)."""
    for key, spec in HOUSEWIFE_LLM_PROMPTS.items():
        assert spec.required_keys, f"prompt {key!r} has no required_keys"
        assert spec.description, f"prompt {key!r} has no description"
