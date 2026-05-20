from __future__ import annotations

from dataclasses import dataclass

from sreda.eval.llm_eval_v2_runner import (
    FakeLLMResponse,
    run_scenario_with_fake_llm,
)
from sreda.eval.llm_eval_v2_scenarios import ExpectedState, ReminderState
from sreda.eval.llm_eval_v2_scenarios import scenario_by_id


@dataclass
class _FakeLLM:
    responses: list[FakeLLMResponse]

    def invoke_turn(self, _messages, _state):
        return self.responses.pop(0)


def schedule_ok_llm() -> _FakeLLM:
    return _FakeLLM(
        responses=[
            FakeLLMResponse(
                tool_calls=(
                    {
                        "name": "schedule_reminder",
                        "args": {
                            "title": "Поймать ежика",
                            "trigger_iso": "2026-05-21T11:00:00+03:00",
                        },
                    },
                ),
                text="Готово, поставила напоминание.",
            )
        ]
    )


def no_tool_success_text_llm() -> _FakeLLM:
    return _FakeLLM(
        responses=[
            FakeLLMResponse(
                tool_calls=(),
                text="Готово, поставила напоминание.",
            )
        ]
    )


def test_runner_resets_fixture_between_runs() -> None:
    scenario = scenario_by_id("one_shot_reminder")

    result1 = run_scenario_with_fake_llm(scenario, fake_llm=schedule_ok_llm())
    result2 = run_scenario_with_fake_llm(scenario, fake_llm=schedule_ok_llm())

    assert result1.before_state == result2.before_state
    assert result1.after_state == result2.after_state
    assert result1.verdict == "PASS"
    assert result2.verdict == "PASS"


def test_runner_rejects_unexpected_tool_sequence() -> None:
    scenario = scenario_by_id("one_shot_reminder")

    result = run_scenario_with_fake_llm(
        scenario,
        fake_llm=no_tool_success_text_llm(),
        provider="mimo-v2.5",
    )

    assert result.verdict == "FAIL"
    assert result.failure_reason == "unexpected_tool_sequence"
    assert result.provider == "mimo-v2.5"


def test_runner_accepts_exact_match_either_sequence() -> None:
    scenario = scenario_by_id("valid_multi_action")
    fake_llm = _FakeLLM(
        responses=[
            FakeLLMResponse(
                tool_calls=(
                    {
                        "name": "schedule_reminder",
                        "args": {
                            "title": "Поймать ежика",
                            "trigger_iso": "2026-05-21T11:00:00+03:00",
                        },
                    },
                    {"name": "list_shopping", "args": {}},
                ),
                text="Готово: напоминание поставила, молоко уже было в покупках.",
            )
        ]
    )

    result = run_scenario_with_fake_llm(scenario, fake_llm=fake_llm)

    assert result.verdict == "PASS"


def test_runner_rejects_final_state_mismatch() -> None:
    scenario = scenario_by_id("one_shot_reminder")
    fake_llm = _FakeLLM(
        responses=[
            FakeLLMResponse(
                tool_calls=(
                    {
                        "name": "schedule_reminder",
                        "args": {
                            "title": "Поймать ежика",
                            "trigger_iso": "2026-05-21T10:00:00+03:00",
                        },
                    },
                ),
                text="Готово, поставила напоминание.",
            )
        ]
    )

    result = run_scenario_with_fake_llm(scenario, fake_llm=fake_llm)

    assert result.verdict == "FAIL"
    assert result.failure_reason == "final_state_mismatch"


def test_runner_rejects_missing_required_clarification() -> None:
    scenario = scenario_by_id("ambiguous_multi_action")
    fake_llm = _FakeLLM(
        responses=[
            FakeLLMResponse(
                tool_calls=({"name": "list_shopping", "args": {}},),
                text="Молоко уже в покупках.",
            )
        ]
    )

    result = run_scenario_with_fake_llm(scenario, fake_llm=fake_llm)

    assert result.verdict == "FAIL"
    assert result.failure_reason == "reply_expectation_failed"


def test_runner_rejects_success_claim_when_forbidden() -> None:
    scenario = scenario_by_id("capability_gap")
    fake_llm = _FakeLLM(
        responses=[
            FakeLLMResponse(
                tool_calls=(),
                text="Готово, буду присылать погоду каждое утро.",
            )
        ]
    )

    result = run_scenario_with_fake_llm(scenario, fake_llm=fake_llm)

    assert result.verdict == "FAIL"
    assert result.failure_reason == "reply_expectation_failed"


def test_runner_returns_fail_for_invalid_tool_args() -> None:
    scenario = scenario_by_id("one_shot_reminder")
    fake_llm = _FakeLLM(
        responses=[
            FakeLLMResponse(
                tool_calls=(
                    {
                        "name": "schedule_reminder",
                        "args": {"title": "Поймать ежика"},
                    },
                ),
                text="Готово, поставила напоминание.",
            )
        ]
    )

    result = run_scenario_with_fake_llm(scenario, fake_llm=fake_llm)

    assert result.verdict == "FAIL"
    assert result.failure_reason == "invalid_tool_args"


def test_update_reminder_preserves_other_reminders() -> None:
    scenario = scenario_by_id("correction_update")
    initial_state = ExpectedState(
        reminders=(
            ReminderState("Пить воду", "2026-05-21T09:00:00+03:00"),
            ReminderState("Поймать ежика", "2026-05-21T10:00:00+03:00"),
        )
    )
    expected_state = ExpectedState(
        reminders=(
            ReminderState("Пить воду", "2026-05-21T09:00:00+03:00"),
            ReminderState("Поймать ежика", "2026-05-21T11:00:00+03:00"),
        )
    )
    fake_llm = _FakeLLM(
        responses=[
            FakeLLMResponse(
                tool_calls=(
                    {
                        "name": "update_reminder",
                        "args": {
                            "title": "Поймать ежика",
                            "trigger_iso": "2026-05-21T11:00:00+03:00",
                        },
                    },
                ),
                text="Готово, перенесла напоминание.",
            )
        ]
    )

    result = run_scenario_with_fake_llm(
        scenario,
        fake_llm=fake_llm,
        initial_state=initial_state,
        expected_state=expected_state,
    )

    assert result.verdict == "PASS"


def test_update_reminder_wrong_title_fails_closed() -> None:
    scenario = scenario_by_id("correction_update")
    fake_llm = _FakeLLM(
        responses=[
            FakeLLMResponse(
                tool_calls=(
                    {
                        "name": "update_reminder",
                        "args": {
                            "title": "Несуществующее",
                            "trigger_iso": "2026-05-21T11:00:00+03:00",
                        },
                    },
                ),
                text="Готово, перенесла напоминание.",
            )
        ]
    )

    result = run_scenario_with_fake_llm(scenario, fake_llm=fake_llm)

    assert result.verdict == "FAIL"
    assert result.failure_reason == "invalid_tool_args"
