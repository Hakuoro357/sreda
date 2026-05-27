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

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sreda.services.tool_schemas.families import FAMILY_HEADERS, Family


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

    import typing
    from typing import get_args, get_origin

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
    write_domains: list[str] = Field(default_factory=list)
    parallel_safe: bool = False
    timeout_seconds: int = Field(default=15, ge=1, le=600)
    side_effect_class: Literal[
        "read_only", "transactional_write", "external_side_effect"
    ] = "transactional_write"
    input_model: type[BaseModel]
    output_model: Any  # Annotated[Union[...], Field(discriminator='status')]
    outcome_examples: list[dict[str, Any]] = Field(default_factory=list)
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
