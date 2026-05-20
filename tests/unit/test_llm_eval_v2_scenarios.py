from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sreda.eval.llm_eval_v2_scenarios import (
    FROZEN_NOW,
    load_core_scenarios,
    scenario_by_id,
    validate_core_scenarios,
)


def test_core_scenarios_are_all_explicit() -> None:
    scenarios = load_core_scenarios()

    assert len(scenarios) == 11
    for scenario in scenarios:
        assert scenario.id
        assert scenario.fixture
        assert scenario.user_messages
        assert scenario.expected_tool_calls_per_turn
        assert scenario.expected_final_state is not None
        assert scenario.expected_reply is not None


def test_no_wrapper_scenarios_in_core_matrix() -> None:
    ids = {scenario.id for scenario in load_core_scenarios()}

    assert "deprecated_negative_grok" not in ids
    assert "rate_limit_network" not in ids
    assert "fallback_timeout" not in ids
    assert "claim_without_tool" not in ids
    assert "second_unbacked_after_retry" not in ids
    assert "malformed_tool_args" not in ids
    assert "tool_choice_schema_probe" not in ids


def test_frozen_clock_is_global_and_concrete() -> None:
    assert FROZEN_NOW == datetime(2026, 5, 20, 16, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def test_schema_validation_accepts_shipped_scenarios() -> None:
    validate_core_scenarios(load_core_scenarios())


def test_valid_multi_action_has_explicit_alternatives() -> None:
    scenario = scenario_by_id("valid_multi_action")

    assert scenario.expected_tool_calls_per_turn == (
        (
            ("schedule_reminder", "add_shopping_items"),
            ("schedule_reminder", "list_shopping"),
        ),
    )


def test_ambiguous_multi_action_is_explicit() -> None:
    scenario = scenario_by_id("ambiguous_multi_action")

    assert scenario.expected_tool_calls_per_turn == (
        (("add_shopping_items",), ("list_shopping",)),
    )
    assert scenario.expected_final_state.reminders == ()
    assert scenario.expected_final_state.shopping == ("молоко",)
    assert scenario.expected_reply.must_ask_reminder_title is True
