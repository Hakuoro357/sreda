"""Scenario catalog for llm_eval_v2 core LLM evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


FROZEN_NOW = datetime(2026, 5, 20, 16, 0, tzinfo=ZoneInfo("Europe/Moscow"))


@dataclass(frozen=True)
class ReminderState:
    title: str
    trigger_iso: str
    recurrence_rule: str | None = None


@dataclass(frozen=True)
class ExpectedState:
    reminders: tuple[ReminderState, ...] = ()
    shopping: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplyExpectation:
    language: str = "ru"
    must_not_claim_success: bool = False
    must_ask_reminder_title: bool = False
    must_refuse_or_clarify: bool = False
    must_report_both_outcomes: bool = False


@dataclass(frozen=True)
class CoreScenario:
    id: str
    fixture: str
    user_messages: tuple[str, ...]
    expected_tool_calls_per_turn: tuple[tuple[tuple[str, ...], ...], ...]
    expected_final_state: ExpectedState
    expected_reply: ReplyExpectation


EMPTY = ExpectedState()
SHOPPING_MILK = ExpectedState(shopping=("молоко",))
HEDGEHOG_1000 = ExpectedState(
    reminders=(ReminderState("Поймать ежика", "2026-05-21T10:00:00+03:00"),)
)
HEDGEHOG_1100 = ExpectedState(
    reminders=(ReminderState("Поймать ежика", "2026-05-21T11:00:00+03:00"),)
)
WATER_DAILY_0900 = ExpectedState(
    reminders=(
        ReminderState(
            "Пить воду",
            "2026-05-21T09:00:00+03:00",
            recurrence_rule="FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
        ),
    )
)


CORE_SCENARIOS: tuple[CoreScenario, ...] = (
    CoreScenario(
        id="chitchat",
        fixture="empty",
        user_messages=("привет",),
        expected_tool_calls_per_turn=(((),),),
        expected_final_state=EMPTY,
        expected_reply=ReplyExpectation(),
    ),
    CoreScenario(
        id="one_shot_reminder",
        fixture="empty",
        user_messages=("поставь напоминание на завтра в 11:00 поймать ежика",),
        expected_tool_calls_per_turn=((("schedule_reminder",),),),
        expected_final_state=HEDGEHOG_1100,
        expected_reply=ReplyExpectation(),
    ),
    CoreScenario(
        id="recurring_past_anchor",
        fixture="empty",
        user_messages=("напоминай каждый день в 09:00 пить воду",),
        expected_tool_calls_per_turn=((("schedule_reminder",),),),
        expected_final_state=WATER_DAILY_0900,
        expected_reply=ReplyExpectation(),
    ),
    CoreScenario(
        id="past_one_shot",
        fixture="empty",
        user_messages=("напомни сегодня в 09:00 проверить тест",),
        expected_tool_calls_per_turn=(((),),),
        expected_final_state=EMPTY,
        expected_reply=ReplyExpectation(
            must_not_claim_success=True,
            must_refuse_or_clarify=True,
        ),
    ),
    CoreScenario(
        id="correction_update",
        fixture="reminder_b",
        user_messages=("нет, лучше завтра в 11:00",),
        expected_tool_calls_per_turn=(
            (("list_reminders", "update_reminder"), ("update_reminder",)),
        ),
        expected_final_state=HEDGEHOG_1100,
        expected_reply=ReplyExpectation(),
    ),
    CoreScenario(
        id="shopping_existing",
        fixture="shopping_a",
        user_messages=("добавь молоко в покупки",),
        expected_tool_calls_per_turn=((("add_shopping_items",), ("list_shopping",)),),
        expected_final_state=SHOPPING_MILK,
        expected_reply=ReplyExpectation(),
    ),
    CoreScenario(
        id="valid_multi_action",
        fixture="shopping_a",
        user_messages=(
            "поставь напоминание на завтра в 11:00 поймать ежика "
            "и добавь еще молоко в покупки",
        ),
        expected_tool_calls_per_turn=(
            (
                ("schedule_reminder", "add_shopping_items"),
                ("schedule_reminder", "list_shopping"),
            ),
        ),
        expected_final_state=ExpectedState(
            reminders=HEDGEHOG_1100.reminders,
            shopping=("молоко",),
        ),
        expected_reply=ReplyExpectation(must_report_both_outcomes=True),
    ),
    CoreScenario(
        id="ambiguous_multi_action",
        fixture="shopping_a",
        user_messages=("поставь напоминание на завтра в 11:00 и добавь молоко в покупки",),
        expected_tool_calls_per_turn=((("add_shopping_items",), ("list_shopping",)),),
        expected_final_state=SHOPPING_MILK,
        expected_reply=ReplyExpectation(must_ask_reminder_title=True),
    ),
    CoreScenario(
        id="capability_gap",
        fixture="empty",
        user_messages=("каждое утро присылай мне актуальную погоду",),
        expected_tool_calls_per_turn=(((),),),
        expected_final_state=EMPTY,
        expected_reply=ReplyExpectation(
            must_not_claim_success=True,
            must_refuse_or_clarify=True,
        ),
    ),
    CoreScenario(
        id="list_read",
        fixture="reminder_a",
        user_messages=("покажи мои напоминания",),
        expected_tool_calls_per_turn=((("list_reminders",),),),
        expected_final_state=ExpectedState(
            reminders=(
                ReminderState("Пить воду", "2026-05-21T09:00:00+03:00"),
            ),
        ),
        expected_reply=ReplyExpectation(),
    ),
    CoreScenario(
        id="multi_turn_correction",
        fixture="empty",
        user_messages=(
            "поставь напоминание на завтра в 10:00 поймать ежика",
            "нет, лучше в 11:00",
        ),
        expected_tool_calls_per_turn=(
            (("schedule_reminder",),),
            (("list_reminders", "update_reminder"), ("update_reminder",)),
        ),
        expected_final_state=HEDGEHOG_1100,
        expected_reply=ReplyExpectation(),
    ),
)


def load_core_scenarios() -> tuple[CoreScenario, ...]:
    return CORE_SCENARIOS


def scenario_by_id(scenario_id: str) -> CoreScenario:
    for scenario in CORE_SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(scenario_id)


def validate_core_scenarios(scenarios: tuple[CoreScenario, ...]) -> None:
    ids: set[str] = set()
    for scenario in scenarios:
        if not scenario.id:
            raise ValueError("scenario id is required")
        if scenario.id in ids:
            raise ValueError(f"duplicate scenario id: {scenario.id}")
        ids.add(scenario.id)
        if not scenario.fixture:
            raise ValueError(f"{scenario.id}: fixture is required")
        if not scenario.user_messages:
            raise ValueError(f"{scenario.id}: user_messages are required")
        if len(scenario.expected_tool_calls_per_turn) != len(scenario.user_messages):
            raise ValueError(f"{scenario.id}: tool expectations must match turn count")
