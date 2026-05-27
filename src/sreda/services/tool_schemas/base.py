"""Base classes for tool input/output schemas + registry (Sub-A1, Epic #74).

``ToolOutput`` — common base; subclasses fix the ``status`` literal that
discriminates the output union.

``ToolOutputContractViolation`` — what the wrapper returns when a tool's
raw ``str`` doesn't match any known pattern (Group 6.5 → triggers
``planner_gaps(gap_type='contract_violation')`` + executor halt).

``ToolSpec`` — registry entry the planner/validator/executor all read.

Invariants enforced here at construction time:

- ``effect='write'`` requires non-empty ``write_domains`` (Group 2 needs
  these for conflict detection between parallel actions)
- ``side_effect_class='external_side_effect'`` is forbidden in MVP
  (Group 6.3 — until compensation/abort design is approved, no tool may
  declare itself as making non-rollback-able external calls)
- ``side_effect_class='read_only'`` requires ``effect='read'`` (consistency)
- ``timeout_seconds`` ∈ [1, 600] (sanity bounds)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sreda.services.tool_schemas.families import FAMILIES, Family


def _direct_field_validator_names(model: type[BaseModel]) -> set[str]:
    """Field names covered by ``@field_validator`` decorators on
    ``model`` itself (NOT recursive). Helper for the public recursive
    walker.
    """

    decorators = getattr(model, "__pydantic_decorators__", None)
    if decorators is None:
        return set()
    field_validators = getattr(decorators, "field_validators", None)
    if not field_validators:
        return set()
    covered: set[str] = set()
    for deco in field_validators.values():
        info = getattr(deco, "info", None)
        fields = getattr(info, "fields", None) if info is not None else None
        if fields:
            covered.update(fields)
    return covered


def _collect_field_validator_names(
    model: type[BaseModel],
    _prefix: str = "",
    _seen: set[type] | None = None,
) -> set[str]:
    """Recursively collect dotted paths to fields covered by
    ``@field_validator`` decorators anywhere in ``model``'s field
    graph — top-level, in nested ``BaseModel`` subclasses, wrapped in
    ``Optional`` / ``Annotated`` / containers.

    Codex Sub-A-77 item #4 R7 MAJOR: the validator's
    ``_validate_nested_basemodel_partial`` path uses TypeAdapter and
    bypasses ``@field_validator`` declared on nested models too. Guard
    must walk every nested ``BaseModel`` to be effective.

    Returns paths like ``"author.full_name"`` so the error message
    points the operator at the exact nested field. Empty set when
    nothing is found.

    Cycle-safe via ``_seen`` (handles self-referential nested models).
    """


    if _seen is None:
        _seen = set()
    if model in _seen:
        return set()
    _seen = _seen | {model}

    paths: set[str] = set()
    for direct in _direct_field_validator_names(model):
        paths.add(f"{_prefix}{direct}" if _prefix else direct)

    for fname, finfo in model.model_fields.items():
        annotation = finfo.annotation
        for nested_cls in _iter_nested_basemodel_types(annotation):
            sub_prefix = f"{_prefix}{fname}." if not _prefix else f"{_prefix}{fname}."
            paths.update(
                _collect_field_validator_names(
                    nested_cls, _prefix=sub_prefix, _seen=_seen
                )
            )
    return paths


def _iter_nested_basemodel_types(annotation: Any):
    """Yield every ``BaseModel`` subclass found by unwrapping
    ``Annotated`` / ``Optional`` / ``Union`` and peeling builtin
    containers (``list[T]`` / ``dict[K,V]`` / ``tuple[T,...]`` / ``set[T]``).

    Used by ``_collect_field_validator_names`` to walk the nested-model
    graph. Stops at non-BaseModel concrete types (won't recurse into
    int, str, etc.). Returns concrete classes only (not generic origins).
    """

    import types as _types
    import typing as _typing
    from typing import get_args, get_origin

    origin = get_origin(annotation)
    if origin is _typing.Annotated:
        for nested in _iter_nested_basemodel_types(get_args(annotation)[0]):
            yield nested
        return
    if origin is _typing.Union or origin is _types.UnionType:
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            yield from _iter_nested_basemodel_types(arg)
        return
    if origin in (list, set, frozenset):
        args = get_args(annotation)
        if args:
            yield from _iter_nested_basemodel_types(args[0])
        return
    if origin is tuple:
        for arg in get_args(annotation):
            if arg is Ellipsis:
                continue
            yield from _iter_nested_basemodel_types(arg)
        return
    if origin is dict:
        args = get_args(annotation)
        if len(args) >= 2:
            # Both K and V — yield BaseModels found in either.
            yield from _iter_nested_basemodel_types(args[0])
            yield from _iter_nested_basemodel_types(args[1])
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation


_STRICT = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
"""Project-wide pydantic config for tool registry schemas.

``arbitrary_types_allowed=True`` is required because ``ToolSpec`` carries
references to ``type[BaseModel]`` and to discriminated-union types (which
pydantic cannot fully introspect as field types in v2).
"""


# Russian-infinitive verb-start check for production ToolSpec.description.
# Required suffixes: -ть, -ться, -ти, -чь (covers «добавить»,
# «отметиться», «найти», «беречь»). Cyrillic-only — Latin would
# require its own ruleset (Codex R1 MINOR #8 fix).
# Used by registry_quality.validate_tool_registry_quality, NOT by
# ToolSpec construction (Codex R1 alternative — separate schema
# from quality policy).
_RUSSIAN_INFINITIVE_FIRST_WORD = re.compile(
    r"^[А-ЯЁ][а-яё\-]*?(?:ть|ться|ти|чь)\b"
)


# Tool name shape: lowercase identifier, digits/underscores after first
# letter. Codex R2 MAJOR #2 — prevents planner-call typos (`Add_Item`
# vs `add_item`), accidental whitespace in JSON keys, and executor
# lookup ambiguity.
_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
"""Strict identifier shape for ``ToolSpec.name``: lowercase letter
followed by lowercase letters / digits / underscores. Codex R2
MAJOR #2 + R3 MINOR #3 — prevents planner-call typos (``Add_Item``
vs ``add_item``), whitespace in JSON keys, and executor-lookup
ambiguity. Matches every entry in ``TOOL_FAMILY_MANIFEST``.
"""


_CONTROL_CHARS = "\n\r\t\x00"
"""Control chars rejected in every prompt-rendered ToolSpec text
field: description, trigger_examples, mutex_notes, domain values.
Codex R7 close-out — single source of truth (was split between
locations in R5/R6, leading to inconsistent ``description``/``\\x00``
handling).
"""


class ToolOutput(BaseModel):
    """Base for tool output schemas.

    Subclasses must override ``status`` with a ``Literal[...]`` value so
    the discriminator union can route results to the correct variant.
    """

    model_config = ConfigDict(extra="forbid")
    status: str


class ToolOutputContractViolation(ToolOutput):
    """Sentinel output returned by the wrapper when a tool's raw output
    matches none of the registered schemas for that tool.

    The executor detects this status, halts the plan, writes a
    ``planner_gaps(gap_type='contract_violation')`` record, and alerts
    the admin. GEPA may use accumulated violations to evolve the
    planner's expectations.
    """

    status: Literal["contract_violation"] = "contract_violation"
    raw_output: str
    tool_name: str
    timestamp: datetime


class ToolSpec(BaseModel):
    """Registry entry describing a tool's contract + scheduling metadata.

    The planner reads ``name``, ``description``, ``input_model``, and
    ``outcome_examples`` to know how to invoke the tool. The validator
    reads ``effect`` + ``read_domains`` + ``write_domains`` +
    ``parallel_safe`` to compute the topological execution graph. The
    executor reads ``timeout_seconds`` + ``side_effect_class`` to wrap
    invocations safely (Group 6.3 — shield + wait_for cancellation).
    """

    model_config = _STRICT

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    family: Family | None = None
    """Closed-taxonomy family for the planner-facing registry text.

    Every production tool SHOULD declare exactly one family from the
    12-value ``Family`` literal (see ``services/tool_schemas/families.py``).
    The registry text renderer groups tools by family and prepends
    anti-pattern headers.

    Optional with default ``None`` to keep this patch genuinely additive
    while Sub-A4 migrates real ToolSpec definitions (Codex R1 MAJOR #3).
    Tools without a declared family surface in the rendered registry's
    final ``ГРУППА: НЕОТНЕСЁННЫЕ (ОШИБКА КОНФИГА)`` block so the gap is
    visible — never silently dropped. A future Sub-B1 CI check will
    fail when this block is non-empty in production.
    """
    effect: Literal["read", "write"]
    read_domains: list[str] = Field(default_factory=list)
    """Every mutable data domain the tool READS from. Each item must
    be a member of the closed Family taxonomy (FAMILIES tuple).

    May contain multiple values when the tool spans families. May
    differ from ``spec.family`` — the latter is purely planner-prompt
    grouping, while domains describe the scheduler's view of what
    data the tool touches (Codex R6 MAJOR).

    Example: ``generate_shopping_from_menu`` has family=`menu`,
    read_domains=[`menu`], write_domains=[`shopping`]."""
    write_domains: list[str] = Field(default_factory=list)
    """Every mutable data domain the tool WRITES to. Each item must
    be a member of FAMILIES. Multi-domain values are mandatory for
    cross-family tools — Sub-A4 must NOT mechanically set
    ``write_domains=[spec.family]`` (would miss real parallel-write
    conflicts).

    Example: ``attach_reminder`` has family=`tasks`,
    write_domains=[`tasks`, `reminders`] — both data stores are
    mutated."""
    parallel_safe: bool = False
    timeout_seconds: int = Field(default=15, ge=1, le=600)
    side_effect_class: Literal[
        "read_only", "transactional_write", "external_side_effect"
    ] = "transactional_write"
    input_model: type[BaseModel]
    output_model: Any  # Annotated[Union[...], Field(discriminator='status')]
    outcome_examples: list[dict[str, Any]] = Field(default_factory=list)
    trigger_examples: list[str] = Field(default_factory=list)
    """Typical user phrases that should route to this tool. Used in
    the planner system prompt to teach the LLM lexical patterns that
    map onto this tool. Sub-A-77 item #6.

    Examples: ``["купи молоко и хлеб", "добавь в покупки яйца",
    "надо купить картошки"]``.

    **Schema-level validation** (ToolSpec construction): each item
    non-empty (post-strip), no leading/trailing whitespace, no
    ``\\n``/``\\r``/``\\t``. Empty list allowed.

    **Production policy validation** (``validate_tool_registry_quality
    (strict=True)``): count ∈ [3, 10], each ≤120 chars, at least one
    Cyrillic character per example.

    Empty list as default keeps test fixtures and partial-migration
    ToolSpecs valid; Sub-A4 populates real values + CI calls the
    quality linter to enforce production policy.
    """
    mutex_notes: list[str] = Field(default_factory=list)
    """Optional short notes for disambiguation with close-sibling
    tools (Codex E-15). Renderer prepends the ⚠ marker — store the
    text WITHOUT it.

    Example for ``mark_shopping_bought``:
    ``["Используй ТОЛЬКО для купленных. Для удаления/коррекции — remove_shopping_items."]``

    **Schema-level validation**: each item non-empty (post-strip),
    no leading/trailing whitespace, no ``\\n``/``\\r``/``\\t``, does
    NOT start with ``⚠`` (renderer owns the marker).

    **Production policy validation** (``strict=True``): count ≤3,
    each ≤200 chars.

    Apply ONLY to tool families with close-sibling confusion
    (mark/remove, complete/cancel/delete). Most standalone tools
    should leave this empty — overpopulating dilutes planner attention.
    """
    allow_field_validators: bool = False
    """Opt-in escape hatch for ``input_model`` with ``@field_validator``
    decorators (Codex Sub-A-77 item #4 R6 MAJOR #1).

    When refs are present in ``action.args``, the plan validator's
    Phase 2 uses ``TypeAdapter(annotation)`` for per-field checks
    which does NOT run model-bound ``@field_validator`` decorators.
    Concrete values that would fail a custom field validator pass
    plan validation if any sibling field is a ref — silent gap.

    Default ``False`` rejects any ``input_model`` that declares
    ``@field_validator``. Use ``Annotated[T, ...]`` / ``Field(...)``
    constraints instead — those flow through TypeAdapter cleanly.

    Set ``True`` only with a code comment explaining why this tool's
    field validators are acceptable as «executor-time only» checks
    (e.g. cross-field rules that genuinely don't fit Field metadata
    AND the planner can't accidentally violate them on a refs-present
    path). The flag forces explicit acknowledgement of the deferred
    check, which is safer than implicit.
    """

    @model_validator(mode="after")
    def _validate_invariants(self) -> ToolSpec:
        # Sub-A-77 item #6: ToolSpec construction-time guards are
        # SCHEMA-level only (basic shape + safety-critical). Production
        # quality policy lives in
        # ``services.tool_schemas.registry_quality.validate_tool_registry_quality``
        # so Sub-A4 / Sub-B1 / CI opt in with strict=True.
        # Codex R1 alternative: separate schema from quality policy.
        # Codex R2 MAJOR #2: name and description ALSO need control-char
        # safety (renderer puts both into the planner prompt).
        if not self.name.strip():
            raise ValueError(
                "ToolSpec.name must be a non-empty (post-strip) string."
            )
        if not _TOOL_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                f"ToolSpec.name {self.name!r} must match "
                f"``[a-z][a-z0-9_]*`` — lowercase identifier, digits / "
                f"underscores allowed after leading letter. Strict shape "
                f"prevents planner-call typos and executor-lookup ambiguity."
            )
        if self.name != self.name.strip():
            raise ValueError(
                f"ToolSpec.name {self.name!r} has leading/trailing "
                f"whitespace — strip before constructing (Codex R2 MINOR #6)."
            )
        if not self.description.strip():
            raise ValueError(
                f"Tool '{self.name}' description must be non-empty (post-strip)."
            )
        if any(c in self.description for c in _CONTROL_CHARS):
            raise ValueError(
                f"Tool '{self.name}' description contains control char "
                f"(\\n/\\r/\\t/\\x00) — must be single-line text "
                f"(renderer puts it in the planner prompt)."
            )
        if self.description != self.description.strip():
            raise ValueError(
                f"Tool '{self.name}' description has leading/trailing "
                f"whitespace — strip before constructing (Codex R2 MINOR #6)."
            )
        # Codex R6 MINOR + R7 close-out: unified control-char block
        # `_CONTROL_CHARS` is module-scope (single source of truth) and
        # applied to description, trigger_examples, mutex_notes, and
        # every domain value.
        if self.trigger_examples:
            for example in self.trigger_examples:
                if not example.strip():
                    raise ValueError(
                        f"Tool '{self.name}' has empty/blank trigger_example."
                    )
                if any(c in example for c in _CONTROL_CHARS):
                    raise ValueError(
                        f"Tool '{self.name}' trigger_example contains "
                        f"control char (\\n/\\r/\\t/\\x00) — must be "
                        f"single-line text (prompt formatting safety)."
                    )
                if example != example.strip():
                    raise ValueError(
                        f"Tool '{self.name}' trigger_example {example!r} "
                        f"has leading/trailing whitespace — strip before "
                        f"adding (renders with awkward spaces in prompt)."
                    )
        for note in self.mutex_notes:
            if not note.strip():
                raise ValueError(
                    f"Tool '{self.name}' has empty/blank mutex_note."
                )
            if any(c in note for c in _CONTROL_CHARS):
                raise ValueError(
                    f"Tool '{self.name}' mutex_note contains control char "
                    f"(\\n/\\r/\\t/\\x00) — must be single-line text "
                    f"(prompt formatting safety)."
                )
            if note != note.strip():
                raise ValueError(
                    f"Tool '{self.name}' mutex_note {note!r} has "
                    f"leading/trailing whitespace — strip before adding."
                )
            if note.lstrip().startswith("⚠"):
                raise ValueError(
                    f"Tool '{self.name}' mutex_note must NOT start with "
                    f"⚠ — the renderer prepends the marker. Got: {note[:60]!r}."
                )
        # Domain list contents (Codex R4 MAJOR #2 + R5 MAJOR + R6 MAJOR):
        # blank/whitespace/typo domains silently break scheduler
        # conflict detection. Each domain must be a non-empty string
        # AND a member of the closed Family taxonomy (FAMILIES tuple)
        # — typo `"shoping"` would otherwise look isolated from the
        # real `shopping` domain.
        #
        # ``read_domains`` / ``write_domains`` describe **every mutable
        # data store the tool touches**, NOT the tool's planner
        # routing family. They are independent dimensions:
        #
        # - ``spec.family`` — single value, planner-prompt grouping
        #   (e.g. ``add_shopping_items`` lives in family `shopping`).
        # - ``spec.write_domains`` — list of touched mutable domains,
        #   may include multiple Family values, may differ from
        #   ``spec.family``.
        #
        # Cross-family examples (Codex R6 MAJOR — must be encoded by
        # Sub-A4 when migrating real ToolSpecs):
        # - ``generate_shopping_from_menu``: family=`menu`,
        #   read_domains=[`menu`], write_domains=[`shopping`].
        # - ``attach_reminder``: family=`tasks`,
        #   write_domains=[`tasks`, `reminders`].
        #
        # Mechanically setting ``write_domains=[spec.family]`` would
        # miss real conflicts (parallel `generate_shopping_from_menu`
        # + `add_shopping_items` would both write `shopping` without
        # the scheduler knowing).
        #
        # For MVP the domain value SET equals FAMILIES; split into a
        # separate `ToolDomain` literal later if needed (Codex R5 alt).
        for kind, domains in (
            ("read_domains", self.read_domains),
            ("write_domains", self.write_domains),
        ):
            for idx, dom in enumerate(domains):
                if not isinstance(dom, str) or not dom.strip():
                    raise ValueError(
                        f"Tool '{self.name}' {kind}[{idx}]={dom!r} "
                        f"must be a non-empty string."
                    )
                if dom != dom.strip():
                    raise ValueError(
                        f"Tool '{self.name}' {kind}[{idx}]={dom!r} "
                        f"has leading/trailing whitespace — strip before "
                        f"adding (silent scheduler-isolation bug otherwise)."
                    )
                if any(c in dom for c in _CONTROL_CHARS):
                    raise ValueError(
                        f"Tool '{self.name}' {kind}[{idx}]={dom!r} "
                        f"contains control chars (\\n/\\r/\\t/\\x00) — "
                        f"domain names must be single-line identifiers."
                    )
                if dom not in FAMILIES:
                    raise ValueError(
                        f"Tool '{self.name}' {kind}[{idx}]={dom!r} "
                        f"is not in the closed Family taxonomy "
                        f"{list(FAMILIES)}. Typos in domains silently "
                        f"break scheduler conflict detection — a write "
                        f"tool with `write_domains=['shoping']` would "
                        f"look isolated from the real `shopping` domain."
                    )
        # Codex R7 close-out MAJOR: effect / write_domains coherence —
        # both directions enforced. `effect='read'` with a non-empty
        # `write_domains` is a contradictory contract — scheduler
        # conflict detection would key off `effect` (serialize as read)
        # or `write_domains` (serialize as write) and either choice
        # picks one side of an impossibility.
        if self.effect == "read" and self.write_domains:
            raise ValueError(
                f"Tool '{self.name}' has effect='read' but "
                f"write_domains={self.write_domains!r}. Read tools must "
                f"have empty write_domains — a non-empty list creates a "
                f"contradictory scheduler contract."
            )
        if self.effect == "write" and not self.write_domains:
            raise ValueError(
                f"Tool '{self.name}' has effect='write' but write_domains "
                f"is empty. Write tools MUST declare their write_domains so "
                f"the validator (Group 2) can detect parallel-safety conflicts."
            )
        if self.side_effect_class == "external_side_effect":
            raise ValueError(
                f"Tool '{self.name}' has side_effect_class='external_side_effect' "
                f"which is FORBIDDEN in MVP (Group 6.3). Until compensation/abort "
                f"design is approved, no tool may make non-rollback-able external "
                f"calls. Use 'transactional_write' if it's a local DB write."
            )
        if not self.allow_field_validators:
            validators = _collect_field_validator_names(self.input_model)
            if validators:
                raise ValueError(
                    f"Tool '{self.name}' input_model "
                    f"{self.input_model.__name__} declares ``@field_validator`` "
                    f"on fields: {sorted(validators)}. Plan validator's "
                    f"refs-present path cannot enforce ``@field_validator`` "
                    f"(only ``Field(...)`` / ``Annotated[T, ...]`` "
                    f"constraints flow through TypeAdapter). Either "
                    f"rewrite the rule as a Field constraint, OR set "
                    f"allow_field_validators=True with a code comment "
                    f"explaining why deferred-to-executor is acceptable."
                )
        if self.side_effect_class == "read_only" and self.effect != "read":
            raise ValueError(
                f"Tool '{self.name}' has side_effect_class='read_only' but "
                f"effect='{self.effect}'. Read-only tools must declare "
                f"effect='read' for the validator to schedule them correctly."
            )
        return self


__all__ = [
    "ToolOutput",
    "ToolOutputContractViolation",
    "ToolSpec",
]
