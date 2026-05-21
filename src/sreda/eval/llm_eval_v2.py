"""Command-line entrypoint for llm_eval_v2."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from sreda.eval.llm_eval_v2_report import build_json_report, build_markdown_report
from sreda.eval.llm_eval_v2_runner import (
    FakeLLMResponse,
    JournalEntry,
    run_scenario_with_fake_llm,
)
from sreda.eval.llm_eval_v2_scenarios import (
    FROZEN_NOW,
    CoreScenario,
    ExpectedState,
    load_core_scenarios,
    validate_core_scenarios,
)
from sreda.services.llm import get_chat_llm


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Sreda llm_eval_v2 core eval.")
    parser.add_argument("--only-provider", default="fake")
    parser.add_argument("--only-scenario")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--markdown-out", required=True)
    parser.add_argument(
        "--full-loop",
        action="store_true",
        help="After stub tool execution, ask the LLM for a final user-visible reply.",
    )
    args = parser.parse_args(argv)

    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")
    provider = _build_provider(args.only_provider)

    scenarios = load_core_scenarios()
    validate_core_scenarios(scenarios)
    if args.only_scenario:
        scenarios = tuple(s for s in scenarios if s.id == args.only_scenario)
        if not scenarios:
            raise SystemExit(f"Unknown scenario: {args.only_scenario}")

    results = []
    for _run_index in range(args.runs):
        for scenario in scenarios:
            results.append(
                run_scenario_with_fake_llm(
                    scenario,
                    fake_llm=provider(scenario),
                    provider=args.only_provider,
                    full_loop=args.full_loop,
                )
            )

    report = build_json_report(
        core_results=results,
        preflight_results=[],
        harness_safety_results=[],
    )
    _write_text(Path(args.json_out), json.dumps(report, ensure_ascii=False, indent=2))
    _write_text(Path(args.markdown_out), build_markdown_report(report))
    return 0


class _HappyPathFakeLLM:
    def __init__(self, scenario: CoreScenario) -> None:
        self._scenario = scenario
        self._turn = 0

    def invoke_turn(self, _messages, _state) -> FakeLLMResponse:
        tool_names = self._scenario.expected_tool_calls_per_turn[self._turn][0]
        self._turn += 1
        return FakeLLMResponse(
            tool_calls=tuple(_tool_call(name, self._scenario.id) for name in tool_names),
            text=_happy_text(self._scenario),
        )

    def invoke_final_response(self, _messages, _state, _journal) -> FakeLLMResponse:
        return FakeLLMResponse(tool_calls=(), text=_happy_text(self._scenario))


class _LiveProviderFactory:
    def __init__(self, provider: str) -> None:
        llm = get_chat_llm(provider=provider)
        if llm is None:
            raise SystemExit(f"Provider is unavailable: {provider}")
        self._bound = llm.bind_tools(_tool_schemas())

    def __call__(self, _scenario: CoreScenario) -> "_LiveScenarioLLM":
        return _LiveScenarioLLM(self._bound)


class _LiveScenarioLLM:
    def __init__(self, bound_llm: Any) -> None:
        self._bound_llm = bound_llm

    def invoke_turn(self, messages, _state) -> FakeLLMResponse:
        response = self._bound_llm.invoke(_messages_for_llm(messages, state=_state))
        return _coerce_response(response)

    def invoke_final_response(
        self,
        messages: tuple[str, ...],
        state: ExpectedState,
        journal: tuple[JournalEntry, ...],
    ) -> FakeLLMResponse:
        response = self._bound_llm.invoke(
            _final_messages_for_llm(messages, state=state, journal=journal)
        )
        return _coerce_response(response)


def _tool_call(name: str, scenario_id: str) -> dict:
    if name == "schedule_reminder":
        trigger = "2026-05-21T10:00:00+03:00" if scenario_id == "multi_turn_correction" else "2026-05-21T11:00:00+03:00"
        if scenario_id == "recurring_past_anchor":
            return {
                "name": name,
                "args": {
                    "title": "Пить воду",
                    "trigger_iso": "2026-05-21T09:00:00+03:00",
                    "recurrence_rule": "FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
                },
            }
        return {
            "name": name,
            "args": {
                "title": "Поймать ежика",
                "trigger_iso": trigger,
            },
        }
    if name == "update_reminder":
        return {
            "name": name,
            "args": {
                "title": "Поймать ежика",
                "trigger_iso": "2026-05-21T11:00:00+03:00",
            },
        }
    if name == "add_shopping_items":
        return {"name": name, "args": {"items": [{"title": "молоко"}]}}
    return {"name": name, "args": {}}


def _happy_text(scenario: CoreScenario) -> str:
    if scenario.expected_reply.must_ask_reminder_title:
        return "Молоко уже в покупках. Что именно напомнить завтра в 11:00?"
    if scenario.expected_reply.must_refuse_or_clarify:
        return "Не могу выполнить это действие. Уточни, пожалуйста."
    if scenario.expected_reply.must_report_both_outcomes:
        return "Готово: напоминание поставила, молоко уже было в покупках."
    return "Готово."


def _build_provider(provider: str):
    if provider == "fake":
        return _HappyPathFakeLLM
    return _LiveProviderFactory(provider)


def _messages_for_llm(
    user_messages: tuple[str, ...],
    *,
    state: ExpectedState = ExpectedState(),
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Ты Среда. Отвечай по-русски. Для действий используй только "
                "tool_calls, не притворяйся что действие сделано без tool.\n"
                f"Текущее время: {FROZEN_NOW.isoformat()}.\n"
                f"Текущее состояние фикстуры: {_state_for_prompt(state)}"
            ),
        }
    ]
    for message in user_messages:
        messages.append({"role": "user", "content": message})
    return messages


def _final_messages_for_llm(
    user_messages: tuple[str, ...],
    *,
    state: ExpectedState,
    journal: tuple[JournalEntry, ...],
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Ты Среда. Отвечай по-русски. Инструменты уже выполнены. "
                "Сейчас нельзя вызывать tools: сформулируй только финальный "
                "ответ пользователю по результатам выполненных инструментов.\n"
                f"Текущее время: {FROZEN_NOW.isoformat()}.\n"
                f"Текущее состояние фикстуры: {_state_for_prompt(state)}"
            ),
        }
    ]
    for message in user_messages:
        messages.append({"role": "user", "content": message})
    messages.append(
        {
            "role": "user",
            "content": (
                "Результаты выполненных инструментов:\n"
                f"{_journal_for_prompt(journal)}\n"
                "Сформулируй финальный ответ пользователю."
            ),
        }
    )
    return messages


def _coerce_response(response: Any) -> FakeLLMResponse:
    raw_tool_calls = _get_response_value(response, "tool_calls") or []
    tool_calls: list[dict[str, Any]] = []
    for call in raw_tool_calls:
        if isinstance(call, dict):
            name = call.get("name") or call.get("function", {}).get("name")
            args = call.get("args") or call.get("function", {}).get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append({"name": name, "args": args})
    content = _get_response_value(response, "content") or ""
    return FakeLLMResponse(tool_calls=tuple(tool_calls), text=str(content))


def _get_response_value(response: Any, name: str) -> Any:
    if isinstance(response, dict):
        return response.get(name)
    return getattr(response, name, None)


def _state_for_prompt(state: ExpectedState) -> str:
    reminders = [
        {
            "title": reminder.title,
            "trigger_iso": reminder.trigger_iso,
            "recurrence_rule": reminder.recurrence_rule,
        }
        for reminder in state.reminders
    ]
    return json.dumps(
        {
            "reminders": reminders,
            "shopping": list(state.shopping),
        },
        ensure_ascii=False,
    )


def _journal_for_prompt(journal: tuple[JournalEntry, ...]) -> str:
    return json.dumps(
        [
            {
                "tool_name": entry.tool_name,
                "args": entry.args,
                "result": entry.result,
            }
            for entry in journal
        ],
        ensure_ascii=False,
    )


def _tool_schemas() -> list[dict[str, Any]]:
    return [
        _tool_schema(
            "schedule_reminder",
            {
                "title": {"type": "string"},
                "trigger_iso": {"type": "string"},
                "recurrence_rule": {"type": "string"},
            },
            required=("title", "trigger_iso"),
        ),
        _tool_schema(
            "update_reminder",
            {
                "title": {"type": "string"},
                "trigger_iso": {"type": "string"},
            },
            required=("trigger_iso",),
        ),
        _tool_schema("list_reminders", {}, required=()),
        _tool_schema(
            "add_shopping_items",
            {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                    },
                }
            },
            required=("items",),
        ),
        _tool_schema("list_shopping", {}, required=()),
    ]


def _tool_schema(
    name: str,
    properties: dict[str, Any],
    *,
    required: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"llm_eval_v2 stub tool: {name}",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
            },
        },
    }


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
