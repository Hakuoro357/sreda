from __future__ import annotations

import json

import pytest

from sreda.eval import llm_eval_v2
from sreda.eval.llm_eval_v2 import _messages_for_llm, main
from sreda.eval.llm_eval_v2_scenarios import HEDGEHOG_1000


def test_cli_fake_provider_writes_reports(tmp_path) -> None:
    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"

    exit_code = main(
        [
            "--only-provider",
            "fake",
            "--runs",
            "2",
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
        ]
    )

    assert exit_code == 0
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert report["summary"]["core_denominator"] == 22
    assert report["summary"]["core_passed"] == 22
    assert "## Core LLM Score" in markdown_out.read_text(encoding="utf-8")


def test_cli_live_provider_uses_get_chat_llm(monkeypatch, tmp_path) -> None:
    calls = []

    class BoundFake:
        def invoke(self, _messages):
            return {
                "tool_calls": [
                    {
                        "name": "schedule_reminder",
                        "args": {
                            "title": "Поймать ежика",
                            "trigger_iso": "2026-05-21T11:00:00+03:00",
                        },
                    }
                ],
                "content": "Готово, поставила напоминание.",
            }

    class ProviderFake:
        def bind_tools(self, tools):
            calls.append([tool["function"]["name"] for tool in tools])
            return BoundFake()

    def fake_get_chat_llm(*, provider=None, **_kwargs):
        calls.append(provider)
        return ProviderFake()

    monkeypatch.setattr(llm_eval_v2, "get_chat_llm", fake_get_chat_llm)
    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"

    exit_code = main(
        [
            "--only-provider",
            "mimo-v2.5",
            "--only-scenario",
            "one_shot_reminder",
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
        ]
    )

    assert exit_code == 0
    assert calls[0] == "mimo-v2.5"
    assert "schedule_reminder" in calls[1]
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert report["summary"]["core_denominator"] == 1
    assert report["summary"]["core_passed"] == 1
    assert report["core_llm"][0]["provider"] == "mimo-v2.5"


def test_cli_rejects_removed_deprecated_negative_flag(tmp_path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--include-deprecated-negative",
                "--json-out",
                str(tmp_path / "report.json"),
                "--markdown-out",
                str(tmp_path / "report.md"),
            ]
        )


def test_cli_rejects_removed_timeout_flag(tmp_path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--timeout",
                "60",
                "--json-out",
                str(tmp_path / "report.json"),
                "--markdown-out",
                str(tmp_path / "report.md"),
            ]
        )


def test_cli_rejects_removed_prod_parity_flag(tmp_path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--prod-parity",
                "--json-out",
                str(tmp_path / "report.json"),
                "--markdown-out",
                str(tmp_path / "report.md"),
            ]
        )


def test_live_prompt_includes_frozen_clock_and_fixture_state() -> None:
    messages = _messages_for_llm(
        ("нет, лучше завтра в 11:00",),
        state=HEDGEHOG_1000,
    )

    system = messages[0]["content"]
    assert "2026-05-20T16:00:00+03:00" in system
    assert "Поймать ежика" in system
    assert "2026-05-21T10:00:00+03:00" in system
