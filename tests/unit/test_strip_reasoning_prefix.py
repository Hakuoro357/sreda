"""Tests for ``strip_reasoning_prefix`` — boundary sanitizer that
removes ReAct-style reasoning markers (``thought\\n``, ``Thinking:``)
from LLM replies before they reach the user. Needed for Gemma-4 tool
mode (verified 2026-04-22) and safe-to-apply on any other model —
idempotent on clean output.
"""

from __future__ import annotations

import pytest

from sreda.services.llm import strip_reasoning_prefix


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Gemma-4 real output format
        ("thought\nВ твоём списке покупок:\n- молоко",
         "В твоём списке покупок:\n- молоко"),
        # Other common variants
        ("Thinking: ok let me answer the question",
         "ok let me answer the question"),
        ("REASONING\n\nfinal answer",
         "final answer"),
        ("analysis:\nthe list is empty",
         "the list is empty"),
        # reflect / reflection
        ("Reflection\n\nГотово, сохранил 3 рецепта.",
         "Готово, сохранил 3 рецепта."),
    ],
)
def test_strips_known_reasoning_markers(raw: str, expected: str) -> None:
    assert strip_reasoning_prefix(raw) == expected


def test_passthrough_for_clean_output() -> None:
    """Normal answer starting with legitimate text must not be touched."""
    clean = "В твоём списке покупок сейчас 3 позиции: молоко, хлеб, яйца."
    assert strip_reasoning_prefix(clean) == clean


def test_idempotent_on_already_clean() -> None:
    """Applying twice equals applying once."""
    raw = "thought\nГотово."
    once = strip_reasoning_prefix(raw)
    twice = strip_reasoning_prefix(once)
    assert once == twice == "Готово."


def test_does_not_strip_legitimate_word_thought_in_sentence() -> None:
    """A reply that genuinely starts with the noun/verb 'thought' in
    a sentence — NOT a ReAct marker — must pass through. The marker
    form is ``thought\\n`` or ``thought:``; prose like 'thought about
    it' doesn't match."""
    prose = "thought about adding tomatoes — should I?"
    # "thought " (space) doesn't match the marker regex, so unchanged.
    assert strip_reasoning_prefix(prose) == prose


def test_empty_and_none_guards() -> None:
    assert strip_reasoning_prefix("") == ""


def test_case_insensitive_match() -> None:
    assert strip_reasoning_prefix("THOUGHT\nok") == "ok"
    assert strip_reasoning_prefix("Thought:\nok") == "ok"


def test_strips_extra_whitespace_after_marker() -> None:
    assert strip_reasoning_prefix("thought\n\n\n  actual answer") == "actual answer"
