"""Tool registry quality policy linter (Sub-A-77 item #6 R1 redesign).

ToolSpec construction (`base.py`) keeps only SCHEMA-level safety
invariants — basic non-blank strings, control-char rejection, side
effect class, write_domains-when-write, @field_validator guard.
``trigger_examples`` and ``mutex_notes`` accept ``[]`` to keep
test fixtures and partial-migration ToolSpecs valid.

This module is the **production-quality gate** Sub-A4 and Sub-B1
call before committing a fully-populated registry. Strict-mode
enforces:

- ``description`` ≥ 60 chars AND starts with Russian infinitive verb
- ``trigger_examples`` count ∈ [3, 10], each item ≤ 120 chars,
  at least one Cyrillic character per example
- ``mutex_notes`` count ∈ [0, 3], each ≤ 200 chars (no ⚠ prefix —
  enforced by ToolSpec)
- ``family`` populated (no ``None``)

The linter returns structured ``Violation`` objects (same shape as
the validator). Callers render with ``render_violations`` for CI
output or wrap in ``InvalidRegistryError`` for fail-loud flow.

Closes Codex R1 MAJOR #1, #3, #6, #7 + MINOR #13 + #12 (kept inline
format for now; bullets is post-MVP A/B if needed).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from sreda.services.tool_schemas.base import (
    _RUSSIAN_INFINITIVE_FIRST_WORD,
    ToolSpec,
)


class RegistryQualityViolation(BaseModel):
    """A registry-quality policy violation. Same shape as validator's
    ``Violation`` (frozen, extra=forbid) so tooling can treat them
    uniformly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    field_path: str | None = None
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class InvalidRegistryError(ValueError):
    """Raised by ``validate_tool_registry_quality(..., raise_on_error=True)``
    when the registry has policy violations. Carries ``.violations`` for
    programmatic access."""

    def __init__(self, violations: list[RegistryQualityViolation]) -> None:
        self.violations = violations
        rendered = "\n".join(
            f"  - {v.tool_name}"
            f"{(':' + v.field_path) if v.field_path else ''}: {v.message}"
            for v in violations
        )
        super().__init__(
            f"Tool registry has {len(violations)} quality violation(s):\n"
            + rendered
        )


# Tunable policy bounds (Codex R1 MAJOR #3).
TRIGGER_EXAMPLES_MIN = 3
TRIGGER_EXAMPLES_MAX = 10
TRIGGER_EXAMPLE_MAX_CHARS = 120
MUTEX_NOTES_MAX = 3
MUTEX_NOTE_MAX_CHARS = 200
DESCRIPTION_MIN_CHARS = 60

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def validate_tool_registry_quality(
    specs: Iterable[ToolSpec],
    *,
    strict: bool = True,
    raise_on_error: bool = False,
) -> list[RegistryQualityViolation]:
    """Run all production-quality checks against ``specs``.

    Args:
        specs: iterable of fully-populated ToolSpecs to lint.
        strict: when True (default), full policy applies. When False,
            only safety-critical checks run (used for partial-migration
            inspection without false alarms during Sub-A4 transition).
        raise_on_error: when True, raise ``InvalidRegistryError`` if
            any violation found. When False (default), return the list.

    Returns:
        list of ``RegistryQualityViolation``. Empty == registry is
        production-ready.

    Raises:
        InvalidRegistryError: when ``raise_on_error=True`` and at
            least one violation exists.
    """

    violations: list[RegistryQualityViolation] = []
    for spec in specs:
        violations.extend(_check_spec(spec, strict=strict))

    if raise_on_error and violations:
        raise InvalidRegistryError(violations)
    return violations


def _check_spec(
    spec: ToolSpec, *, strict: bool
) -> Iterable[RegistryQualityViolation]:
    """Yield all policy violations for one ToolSpec."""

    # Family must be declared in production (Sub-A4 migration target).
    if strict and spec.family is None:
        yield RegistryQualityViolation(
            tool_name=spec.name,
            field_path="family",
            code="missing_family",
            message=(
                f"family is not declared. Every production tool must "
                f"belong to one of the 12 families — see "
                f"docs/architecture/tool-family-taxonomy.md."
            ),
        )

    # Description policy.
    yield from _check_description(spec, strict=strict)

    # Trigger examples policy.
    yield from _check_trigger_examples(spec, strict=strict)

    # Mutex notes policy.
    yield from _check_mutex_notes(spec, strict=strict)


def _check_description(
    spec: ToolSpec, *, strict: bool
) -> Iterable[RegistryQualityViolation]:
    text = spec.description.strip()
    if strict and len(text) < DESCRIPTION_MIN_CHARS:
        yield RegistryQualityViolation(
            tool_name=spec.name,
            field_path="description",
            code="description_too_short",
            message=(
                f"description is {len(text)} chars; production policy "
                f"requires ≥ {DESCRIPTION_MIN_CHARS}. Add lexical "
                f"hints («Используй для запросов вида «X», «Y»»)."
            ),
        )
    if strict and not _RUSSIAN_INFINITIVE_FIRST_WORD.match(text):
        yield RegistryQualityViolation(
            tool_name=spec.name,
            field_path="description",
            code="description_not_infinitive",
            message=(
                f"description must START with a Russian infinitive verb "
                f"(-ть / -ться / -ти / -чь): «Добавить...», «Найти...», "
                f"«Отметить...». Got first chars: {text[:40]!r}."
            ),
        )


def _check_trigger_examples(
    spec: ToolSpec, *, strict: bool
) -> Iterable[RegistryQualityViolation]:
    examples = spec.trigger_examples
    if strict and len(examples) < TRIGGER_EXAMPLES_MIN:
        yield RegistryQualityViolation(
            tool_name=spec.name,
            field_path="trigger_examples",
            code="too_few_trigger_examples",
            message=(
                f"got {len(examples)} examples; need ≥ "
                f"{TRIGGER_EXAMPLES_MIN} (typical + rewording + follow-up "
                f"to teach the planner the variation space)."
            ),
        )
    if len(examples) > TRIGGER_EXAMPLES_MAX:
        yield RegistryQualityViolation(
            tool_name=spec.name,
            field_path="trigger_examples",
            code="too_many_trigger_examples",
            message=(
                f"got {len(examples)} examples; cap is "
                f"{TRIGGER_EXAMPLES_MAX}. Prompt budget + planner "
                f"attention are the reason for these fields — keep "
                f"them tight."
            ),
        )
    for idx, example in enumerate(examples):
        if len(example) > TRIGGER_EXAMPLE_MAX_CHARS:
            yield RegistryQualityViolation(
                tool_name=spec.name,
                field_path=f"trigger_examples[{idx}]",
                code="trigger_example_too_long",
                message=(
                    f"example {example[:40]!r}... is {len(example)} "
                    f"chars; cap is {TRIGGER_EXAMPLE_MAX_CHARS}. Long "
                    f"examples bloat the cached prefix."
                ),
            )
        if strict and not _CYRILLIC_RE.search(example):
            yield RegistryQualityViolation(
                tool_name=spec.name,
                field_path=f"trigger_examples[{idx}]",
                code="trigger_example_not_russian",
                message=(
                    f"example {example!r} has no Cyrillic characters. "
                    f"This is a Russian Telegram assistant — examples "
                    f"directly teach lexical routing for Russian phrases."
                ),
            )


def _check_mutex_notes(
    spec: ToolSpec, *, strict: bool
) -> Iterable[RegistryQualityViolation]:
    notes = spec.mutex_notes
    if len(notes) > MUTEX_NOTES_MAX:
        yield RegistryQualityViolation(
            tool_name=spec.name,
            field_path="mutex_notes",
            code="too_many_mutex_notes",
            message=(
                f"got {len(notes)} mutex notes; cap is "
                f"{MUTEX_NOTES_MAX}. Disambiguation should target the "
                f"single closest sibling pair, not catalog every distinction."
            ),
        )
    for idx, note in enumerate(notes):
        if len(note) > MUTEX_NOTE_MAX_CHARS:
            yield RegistryQualityViolation(
                tool_name=spec.name,
                field_path=f"mutex_notes[{idx}]",
                code="mutex_note_too_long",
                message=(
                    f"note {note[:40]!r}... is {len(note)} chars; cap "
                    f"is {MUTEX_NOTE_MAX_CHARS}. Long notes dilute "
                    f"planner attention."
                ),
            )


__all__ = [
    "DESCRIPTION_MIN_CHARS",
    "InvalidRegistryError",
    "MUTEX_NOTES_MAX",
    "MUTEX_NOTE_MAX_CHARS",
    "RegistryQualityViolation",
    "TRIGGER_EXAMPLES_MAX",
    "TRIGGER_EXAMPLES_MIN",
    "TRIGGER_EXAMPLE_MAX_CHARS",
    "validate_tool_registry_quality",
]
