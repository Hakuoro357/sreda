from __future__ import annotations

from sreda.runtime.handlers import build_system_prompt
from sreda.services.housewife_persona import (
    PERSONA_TENDER_CARE,
)


def test_housewife_prompt_includes_selected_persona_overlay() -> None:
    prompt = build_system_prompt(
        "housewife_assistant",
        persona_preset=PERSONA_TENDER_CARE,
    )

    assert "PERSONA PRESET: tender_care" in prompt
    assert "солнышко" in prompt
    assert prompt.index("PERSONA PRESET: tender_care") < prompt.index(
        "строгая дисциплина tool-calls"
    )


def test_housewife_prompt_defaults_to_warm_persona_overlay() -> None:
    prompt = build_system_prompt("housewife_assistant")

    assert "PERSONA PRESET: warm_practical" in prompt
    assert "PERSONA PRESET: tender_care" not in prompt


def test_non_housewife_prompt_does_not_include_persona_overlay() -> None:
    prompt = build_system_prompt(None, persona_preset=PERSONA_TENDER_CARE)

    assert "PERSONA PRESET:" not in prompt
