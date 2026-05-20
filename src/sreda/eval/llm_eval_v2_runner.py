"""Offline runner for llm_eval_v2 core scenarios."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from sreda.eval.llm_eval_v2_scenarios import (
    CoreScenario,
    ExpectedState,
    HEDGEHOG_1000,
    ReminderState,
    SHOPPING_MILK,
)


@dataclass(frozen=True)
class FakeLLMResponse:
    tool_calls: tuple[dict[str, Any], ...]
    text: str


@dataclass(frozen=True)
class JournalEntry:
    tool_name: str
    args: dict[str, Any]
    result: str


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    verdict: str
    failure_reason: str | None
    before_state: ExpectedState
    after_state: ExpectedState
    tool_calls_per_turn: tuple[tuple[str, ...], ...]
    journal: tuple[JournalEntry, ...]
    final_text: str
    provider: str = "fake"


class FakeLLM(Protocol):
    def invoke_turn(self, messages: tuple[str, ...], state: ExpectedState) -> FakeLLMResponse:
        ...

def run_scenario_with_fake_llm(
    scenario: CoreScenario,
    *,
    fake_llm: FakeLLM,
    provider: str = "fake",
    initial_state: ExpectedState | None = None,
    expected_state: ExpectedState | None = None,
) -> ScenarioResult:
    state = initial_state or _fixture_state(scenario.fixture)
    target_state = expected_state or scenario.expected_final_state
    before_state = deepcopy(state)
    journal: list[JournalEntry] = []
    observed_calls: list[tuple[str, ...]] = []
    final_text = ""

    for turn_index, _user_text in enumerate(scenario.user_messages):
        response = fake_llm.invoke_turn(scenario.user_messages[: turn_index + 1], state)
        final_text = response.text
        names = tuple(str(call.get("name", "")) for call in response.tool_calls)
        observed_calls.append(names)
        if not _matches_any_expected(names, scenario.expected_tool_calls_per_turn[turn_index]):
            return ScenarioResult(
                scenario_id=scenario.id,
                verdict="FAIL",
                failure_reason="unexpected_tool_sequence",
                before_state=before_state,
                after_state=state,
                tool_calls_per_turn=tuple(observed_calls),
                journal=tuple(journal),
                final_text=final_text,
                provider=provider,
            )
        for call in response.tool_calls:
            try:
                state, entry = _apply_tool(state, call)
            except (KeyError, TypeError, ValueError) as exc:
                return ScenarioResult(
                    scenario_id=scenario.id,
                    verdict="FAIL",
                    failure_reason="invalid_tool_args",
                    before_state=before_state,
                    after_state=state,
                    tool_calls_per_turn=tuple(observed_calls),
                    journal=tuple(journal),
                    final_text=f"{final_text}\n\ninvalid_tool_args:{type(exc).__name__}",
                    provider=provider,
                )
            journal.append(entry)

    if state != target_state:
        return ScenarioResult(
            scenario_id=scenario.id,
            verdict="FAIL",
            failure_reason="final_state_mismatch",
            before_state=before_state,
            after_state=state,
            tool_calls_per_turn=tuple(observed_calls),
            journal=tuple(journal),
            final_text=final_text,
            provider=provider,
        )
    if not _reply_matches(final_text, scenario):
        return ScenarioResult(
            scenario_id=scenario.id,
            verdict="FAIL",
            failure_reason="reply_expectation_failed",
            before_state=before_state,
            after_state=state,
            tool_calls_per_turn=tuple(observed_calls),
            journal=tuple(journal),
            final_text=final_text,
            provider=provider,
        )

    return ScenarioResult(
        scenario_id=scenario.id,
        verdict="PASS",
        failure_reason=None,
        before_state=before_state,
        after_state=state,
        tool_calls_per_turn=tuple(observed_calls),
        journal=tuple(journal),
        final_text=final_text,
        provider=provider,
    )


def _fixture_state(fixture: str) -> ExpectedState:
    if fixture == "empty":
        return ExpectedState()
    if fixture == "shopping_a":
        return SHOPPING_MILK
    if fixture == "reminder_b":
        return HEDGEHOG_1000
    if fixture == "reminder_a":
        return ExpectedState(
            reminders=(ReminderState("Пить воду", "2026-05-21T09:00:00+03:00"),)
        )
    raise ValueError(f"unknown fixture: {fixture}")


def _matches_any_expected(observed: tuple[str, ...], alternatives: tuple[tuple[str, ...], ...]) -> bool:
    return observed in alternatives


def _apply_tool(state: ExpectedState, call: dict[str, Any]) -> tuple[ExpectedState, JournalEntry]:
    name = str(call.get("name", ""))
    args = dict(call.get("args") or {})
    if name == "schedule_reminder":
        reminder = ReminderState(
            title=str(args["title"]),
            trigger_iso=str(args["trigger_iso"]),
            recurrence_rule=args.get("recurrence_rule"),
        )
        new_state = ExpectedState(
            reminders=state.reminders + (reminder,),
            shopping=state.shopping,
        )
        return new_state, JournalEntry(name, args, "ok:scheduled")
    if name == "update_reminder":
        title = str(args.get("title") or "")
        trigger_iso = str(args["trigger_iso"])
        if not state.reminders:
            raise ValueError("no reminder to update")
        target_index = _find_reminder_index(state.reminders, title)
        target = state.reminders[target_index]
        updated = ReminderState(
            title=title or target.title,
            trigger_iso=trigger_iso,
            recurrence_rule=args.get("recurrence_rule", target.recurrence_rule),
        )
        reminders = tuple(
            updated if index == target_index else reminder
            for index, reminder in enumerate(state.reminders)
        )
        new_state = ExpectedState(
            reminders=reminders,
            shopping=state.shopping,
        )
        return new_state, JournalEntry(name, args, "ok:updated")
    if name == "add_shopping_items":
        items = tuple(_normalise_item(item) for item in args.get("items", ()))
        merged = tuple(dict.fromkeys(state.shopping + items))
        return ExpectedState(reminders=state.reminders, shopping=merged), JournalEntry(
            name, args, "ok:added"
        )
    if name == "list_shopping":
        return state, JournalEntry(name, args, "ok:list")
    if name == "list_reminders":
        return state, JournalEntry(name, args, "ok:list")
    raise ValueError(f"unsupported tool: {name}")


def _normalise_item(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("title", "")).strip().lower()
    return str(item).strip().lower()


def _find_reminder_index(reminders: tuple[ReminderState, ...], title: str) -> int:
    if title:
        needle = title.casefold()
        for index, reminder in enumerate(reminders):
            if reminder.title.casefold() == needle:
                return index
        raise ValueError(f"reminder title not found: {title}")
    if len(reminders) == 1:
        return 0
    raise ValueError("reminder title is required when multiple reminders exist")


def _reply_matches(text: str, scenario: CoreScenario) -> bool:
    reply = scenario.expected_reply
    text_l = (text or "").lower()
    if reply.must_ask_reminder_title and not _looks_like_question_about_reminder_title(text_l):
        return False
    if reply.must_not_claim_success and _looks_like_success_claim(text_l):
        return False
    if reply.must_refuse_or_clarify and not _looks_like_refusal_or_clarification(text_l):
        return False
    if reply.must_report_both_outcomes:
        has_reminder = "напомин" in text_l
        has_shopping = "покуп" in text_l or "молок" in text_l
        if not (has_reminder and has_shopping):
            return False
    return True


def _looks_like_question_about_reminder_title(text: str) -> bool:
    return "?" in text and any(word in text for word in ("что", "о чем", "текст")) and "напом" in text


def _looks_like_success_claim(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "готово",
            "поставила",
            "добавила",
            "создала",
            "буду присылать",
            "будет приходить",
        )
    )


def _looks_like_refusal_or_clarification(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "не могу",
            "не умею",
            "уточни",
            "уточните",
            "что именно",
            "нужно уточнить",
        )
    )
