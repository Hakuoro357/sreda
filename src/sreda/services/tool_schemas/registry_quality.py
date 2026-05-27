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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    """Run quality checks against ``specs``.

    Three check tiers (Codex R2 MINOR #7 + R3 MINOR #4):

    **Schema-safety re-check (BOTH modes)** — defensive validation of
    prompt-safety string invariants that ``ToolSpec`` construction
    enforces. Catches ``model_copy(update=...)`` bypasses and direct
    attribute mutation. Always runs.

    **Production policy (strict mode only)** — completeness +
    prompt-budget rules:
    - description ≥60 chars + Russian infinitive verb start
    - trigger_examples count ∈ [3, 10]
    - trigger_examples each ≥1 Cyrillic character
    - family populated

    **Prompt-budget caps (BOTH modes)** — length caps that prevent
    blowing out the cached prefix even in soft mode:
    - trigger_examples each ≤120 chars
    - mutex_notes count ≤3, each ≤200 chars

    Args:
        specs: iterable of ToolSpecs to lint.
        strict: when True (default), full production policy applies.
            When False, only schema-safety + budget caps run — used
            during Sub-A4 partial migration so an in-progress registry
            doesn't false-alarm on completeness rules.
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
    """Yield all policy violations for one ToolSpec.

    Codex R4 MAJOR #1: schema-safety re-check is TERMINAL for the spec
    — if any schema violation surfaces (the spec was bypassed via
    model_copy / direct mutation), policy checks would dereference
    raw fields and crash with AttributeError / TypeError before the
    public API returns. Short-circuit instead.
    """

    # Schema-safety re-check — applies in BOTH strict and non-strict
    # modes since these are prompt-breaking issues, not production-
    # policy quality.
    schema_violations = list(_recheck_schema_safety(spec))
    if schema_violations:
        yield from schema_violations
        return  # spec is unsafe — policy checks would crash on raw fields

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


def _recheck_schema_safety(
    spec: ToolSpec,
) -> Iterable[RegistryQualityViolation]:
    """Defensive re-validation of ALL ToolSpec construction invariants
    — catches ``model_copy(update=...)`` and direct attribute mutation
    bypasses (Codex R2 MAJOR #3, R3 MAJOR #1+#2).

    Strategy: dump and reconstruct via ``ToolSpec.model_validate``.
    This runs every construction-time guard (prompt-string safety,
    write_domains for effect=write, external_side_effect ban,
    read_only/effect coupling, timeout bounds, @field_validator,
    name regex, etc.) — there's a single source of truth (ToolSpec's
    own validators) instead of a per-field mirror that drifts as new
    invariants are added.

    Type-safety bonus (R3 MAJOR #2): pydantic raises ``ValidationError``
    for wrong types (``description=None``, ``name=123``,
    ``trigger_examples=[None]``) and we convert that into structured
    violations. No ``AttributeError``/``TypeError`` leaks through the
    public API.
    """

    # Pull a safe tool-name display string for the violation rows
    # (the spec's name might be None / int / blank after mutation).
    safe_name = spec.name if isinstance(spec.name, str) and spec.name else "<invalid>"

    try:
        dumped = spec.model_dump()
    except Exception as exc:  # noqa: BLE001 — defensive, surface as violation
        yield RegistryQualityViolation(
            tool_name=safe_name,
            field_path=None,
            code="schema_safety_violation",
            message=(
                f"spec.model_dump() failed — spec is in an unrecoverable "
                f"state, probably mutated through non-pydantic paths: {exc!r}."
            ),
        )
        return

    try:
        ToolSpec.model_validate(dumped)
    except ValidationError as exc:
        for err in exc.errors():
            loc = err.get("loc") or ()
            field_path = ".".join(str(p) for p in loc) if loc else None
            yield RegistryQualityViolation(
                tool_name=safe_name,
                field_path=field_path,
                code="schema_safety_violation",
                message=(
                    f"{err.get('msg', 'schema invariant violated')}. "
                    f"Was the spec built via model_copy(update=...) or "
                    f"direct attribute mutation?"
                ),
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        yield RegistryQualityViolation(
            tool_name=safe_name,
            field_path=None,
            code="schema_safety_violation",
            message=(
                f"ToolSpec.model_validate raised non-ValidationError "
                f"{type(exc).__name__}: {exc!r}. Spec is corrupt."
            ),
        )


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


def assert_production_registry_quality(specs: Iterable[ToolSpec]) -> None:
    """Sub-A4 / Sub-B1 / CI acceptance gate (Codex R2 MAJOR #1).

    Convenience wrapper for the strict + raise-on-error path. The
    production tool-registry build pipeline calls this before
    committing the registry to the planner system prompt. CI uses it
    to fail builds when a tool ships without trigger_examples, with
    a short description, or with an unfamilied/invalid shape.

    Sub-A4 will wire this into the build step that assembles real
    ToolSpec instances from the housewife_chat_tools migration. Until
    Sub-A4 lands, the function is callable but has no production
    callsite — explicit invocation by Sub-A4 author is the acceptance
    blocker, documented in ``docs/architecture/tool-family-taxonomy.md``.

    Use ``validate_tool_registry_quality`` directly for inspection
    flows (returning a list) or partial-migration soft mode (strict=False).
    """

    validate_tool_registry_quality(
        specs, strict=True, raise_on_error=True
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
    "assert_production_registry_quality",
    "validate_tool_registry_quality",
]
