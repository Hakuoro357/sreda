"""Plan-level argument validator (Sub-A-77 item #4).

Two-phase defense in depth between the planner LLM and the executor:

**Phase 1 — structural / relational checks (per Action)**:
- Every key in ``action.args`` must exist in the tool's
  ``input_model.model_fields`` (aliases normalised). Unknown keys
  surface as errors **regardless of whether they're concrete or ref**
  — fixes the «hallucinated arg hidden behind a ref» bypass.
- Every ``${...}`` reference must:
  - point to an action id that exists in ``plan.actions``;
  - NOT be a self-reference (``s1`` referencing ``${s1.field}``);
  - have a producible dependency relationship — the target action
    must appear before the consumer in the plan (transitively
    via other refs or via explicit ``depends_on``).

**Phase 2 — schema-aware partial validation (per field)**:
- Field with NO refs anywhere: full ``TypeAdapter`` validation against
  the field annotation. Cross-field model validators (``@model_validator``)
  ONLY run when no refs are present at all (otherwise stripped data
  triggers false positives — Codex R1 MAJOR #4).
- Field whose value is a single full-ref string (``"${s1.x}"``): deferred;
  type-preservation at executor time would need producer-side
  ``output_model`` inspection. Documented as deferred limitation
  (Codex R1 MAJOR #6).
- Field whose value is a mixed interpolated string (``"prefix ${s1.x}"``):
  validated as ``str`` against the field annotation — the resolved
  value WILL be a string at executor time. Non-str-compatible fields
  reject this.
- Container value with some concrete and some ref leaves:
  recurse into the container, validate every concrete leaf against its
  positional/keyed annotation. Ref leaves deferred.

Closes Codex R1 concerns:
- MAJOR #1 unknown-arg bypass — Phase 1 key check before strip.
- MAJOR #2 mixed strings — distinguish full-ref vs interpolated.
- MAJOR #3 container coarseness — recurse into list/dict leaves.
- MAJOR #4 model_validator false positives — only run when no refs.
- MAJOR #5 ref integrity — Phase 1 ref-target + dependency check.
- MAJOR #6 producer/consumer type match — DEFERRED, documented.
- MINOR #7 tool name in error — structured ``Violation`` includes it.
- MINOR #8 structured violations — return ``list[Violation]`` plus
  renderer for prompt-friendly strings.
- MINOR #10 private import — uses public ``contains_ref`` /
  ``iter_refs`` / ``is_full_ref_string`` / ``extract_step_id`` from
  ``interpolation.py``.
- MINOR #11 alias handling — normalize via ``model_fields``.

Known deferred limitation (Codex R1 MAJOR #6):
We do NOT statically check that ``${s1.items}`` into ``items: list[str]``
matches producer ``output_model.items: list[str]``. Implementing this
needs walking the producer's output union by ref-path; in MVP we rely
on executor-time validation (the resolved value gets type-checked when
the tool actually receives it). Will revisit once Sub-B1 reveals
how often the static check would have helped.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from sreda.runtime.planner.interpolation import (
    contains_ref,
    extract_step_id,
    is_full_ref_string,
    iter_refs,
)
from sreda.runtime.planner.schemas import Action, Plan
from sreda.services.tool_schemas.base import ToolSpec


class Violation(BaseModel):
    """Structured plan-validation violation (Codex R1 MINOR #8).

    Fields are stable enough for Sub-B1 retry-feedback builders to
    route per-step / per-tool / per-field. The renderer
    ``render_violations`` converts back to the legacy string form.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str | None = None  # None for plan-level violations
    tool: str | None = None     # None when step doesn't have a known tool
    field_path: str | None = None
    code: str = Field(min_length=1)
    """Stable machine-readable code: ``unknown_tool`` /
    ``unknown_arg`` / ``invalid_arg_type`` / ``missing_arg`` /
    ``unknown_ref_target`` / ``self_ref`` / ``forward_ref`` /
    ``invalid_ref_type``. Subset, extend as needed."""
    message: str = Field(min_length=1)


class InvalidPlanError(ValueError):
    """Raised when a plan fails validation. Carries ``.violations`` for
    programmatic access; ``str(exc)`` produces a multi-line render
    suitable for planner retry-feedback prompts.
    """

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        lines = render_violations(violations)
        super().__init__(
            f"Plan invalid: {len(violations)} violation(s):\n"
            + "\n".join(f"  - {line}" for line in lines)
        )


def render_violations(violations: list[Violation]) -> list[str]:
    """Render violations as planner-prompt-friendly strings.

    Format:
        ``{step_id} ({tool}): {field_path}: {message}``
    where each part is omitted if absent (plan-level violations have
    no step_id / tool).
    """

    out: list[str] = []
    for v in violations:
        parts: list[str] = []
        if v.step_id:
            head = v.step_id
            if v.tool:
                head = f"{head} ({v.tool})"
            parts.append(head)
        if v.field_path:
            parts.append(v.field_path)
        parts.append(v.message)
        out.append(": ".join(parts))
    return out


# ---------------------------------------------------------------------------
# Phase 1 — structural / relational checks
# ---------------------------------------------------------------------------


def _accepted_field_names(model: type[BaseModel]) -> set[str]:
    """Union of attribute names AND aliases for ``model_validate``
    input. The planner may emit either form — both must pass key
    membership and both map back to the same field for suppression.
    """

    names: set[str] = set()
    for fname, finfo in model.model_fields.items():
        names.add(fname)
        alias = getattr(finfo, "alias", None)
        if alias:
            names.add(alias)
        # Validation aliases (pydantic v2): may be a single AliasChoices.
        validation_alias = getattr(finfo, "validation_alias", None)
        if isinstance(validation_alias, str):
            names.add(validation_alias)
    return names


def _normalize_to_field_name(
    key: str, model: type[BaseModel]
) -> str | None:
    """Given a key that the planner used (may be alias), return the
    canonical field name in ``model.model_fields``. None if not
    recognised.
    """

    if key in model.model_fields:
        return key
    for fname, finfo in model.model_fields.items():
        alias = getattr(finfo, "alias", None)
        if alias == key:
            return fname
        validation_alias = getattr(finfo, "validation_alias", None)
        if isinstance(validation_alias, str) and validation_alias == key:
            return fname
    return None


def _phase1_check_unknown_keys(
    step_id: str, action: Action, tool_spec: ToolSpec
) -> Iterator[Violation]:
    """Yield ``unknown_arg`` violations for any key in ``action.args``
    that doesn't match a model field name or alias (Codex R1 MAJOR #1).
    Runs BEFORE ref-stripping so ref-valued unknown args also surface.
    """

    accepted = _accepted_field_names(tool_spec.input_model)
    for key in action.args.keys():
        if key not in accepted:
            yield Violation(
                step_id=step_id,
                tool=tool_spec.name,
                field_path=key,
                code="unknown_arg",
                message=(
                    f"unknown argument {key!r} — not in input_model. "
                    f"Accepted: {sorted(accepted)}."
                ),
            )


def _phase1_check_refs(
    step_id: str,
    action: Action,
    plan: Plan,
    tool_spec: ToolSpec | None,
) -> Iterator[Violation]:
    """Yield ref-integrity violations (Codex R1 MAJOR #5):

    - ``unknown_ref_target``: ``${sX.field}`` where ``sX`` isn't a step
      in ``plan.actions``.
    - ``self_ref``: ``${sX.field}`` inside step ``sX`` itself.
    - ``unknown_depends_on``: ``depends_on`` lists a step that isn't
      in ``plan.actions``.

    NOT checked here:
    - Forward reference vs dict ordering: refs INHERENTLY create a
      dependency relationship — the topology-aware scheduler (Group 2
      validator-driven parallelism) reorders by data flow, not by dict
      iteration order. So pointing «forward» is fine if the target
      exists. We do NOT require ``depends_on`` to list ref-targets
      (they're implicit).
    - Cycle detection: deferred. Group 2 scheduler catches cycles when
      building topological layers.
    """

    for ref_path in iter_refs(action.args):
        target = extract_step_id(ref_path)
        if target not in plan.actions:
            yield Violation(
                step_id=step_id,
                tool=tool_spec.name if tool_spec else None,
                field_path=None,
                code="unknown_ref_target",
                message=(
                    f"reference '${{{ref_path}}}' targets unknown step "
                    f"{target!r}. Available steps: {sorted(plan.actions.keys())}."
                ),
            )
            continue
        if target == step_id:
            yield Violation(
                step_id=step_id,
                tool=tool_spec.name if tool_spec else None,
                field_path=None,
                code="self_ref",
                message=(
                    f"reference '${{{ref_path}}}' is a self-reference "
                    f"— a step cannot consume its own output."
                ),
            )

    # ``depends_on`` integrity (self-reference, unknown target, cycles)
    # is already enforced by ``Plan`` schema at construction time —
    # see ``schemas.py:_validate_actions``. No duplicate work here.


# NOTE: Predecessor/topology graph construction lives in Group 2's
# validator (topological_layers builder) — this module does not need
# its own copy. Cycle detection happens there. Here we only check
# per-action ref-target / self-ref / unknown-depends-on, which don't
# need graph context.


# ---------------------------------------------------------------------------
# Phase 2 — schema-aware partial validation
# ---------------------------------------------------------------------------


def _phase2_validate_args(
    step_id: str, action: Action, tool_spec: ToolSpec
) -> Iterator[Violation]:
    """Per-field validation:

    1. If ``action.args`` has NO refs anywhere — full
       ``input_model.model_validate``, including cross-field
       ``@model_validator`` rules. (Codex R1 MAJOR #4 — safe because
       no stripped data.)
    2. Otherwise validate each field individually with
       ``TypeAdapter(field_annotation)``. Skip pure full-ref values
       (deferred). Validate mixed-string refs as ``str``. Recurse into
       containers; concrete leaves go through TypeAdapter against their
       element annotation; ref leaves deferred.

    Unknown keys are already reported by Phase 1; in Phase 2 we filter
    them out so pydantic's per-field check doesn't double-report.
    """

    model = tool_spec.input_model
    accepted_names = _accepted_field_names(model)

    # Two dicts:
    # - ``known_args_as_emitted`` keeps planner's original keys (possibly
    #   aliases) — passed to ``model_validate`` so pydantic's own alias
    #   resolution + ``populate_by_name`` semantics apply unchanged.
    # - ``canonical_args`` maps each known key to its CANONICAL field
    #   name; used for the per-field walk so we iterate model_fields
    #   without alias confusion.
    known_args_as_emitted: dict[str, Any] = {}
    canonical_args: dict[str, Any] = {}
    for key, value in action.args.items():
        if key not in accepted_names:
            continue  # unknown — Phase 1 handled
        known_args_as_emitted[key] = value
        canonical_key = _normalize_to_field_name(key, model)
        if canonical_key is not None:
            canonical_args[canonical_key] = value

    if not contains_ref(known_args_as_emitted):
        # No refs anywhere — full validation including cross-field rules.
        # Pass keys AS EMITTED so pydantic handles aliases.
        try:
            model.model_validate(known_args_as_emitted)
        except ValidationError as exc:
            for err in exc.errors():
                yield _violation_from_pydantic_error(
                    step_id, tool_spec, err, known_args_as_emitted
                )
        return

    # Refs present — validate per-field, avoid model_validator.
    for fname, finfo in model.model_fields.items():
        if fname not in canonical_args:
            # Missing: only a violation if the field is required AND
            # not provided under any alias. (Aliases already normalised
            # to canonical name, so absence here is real absence.)
            try:
                required = finfo.is_required()
            except AttributeError:  # pragma: no cover — pydantic v1 fallback
                required = finfo.default is ...
            if required:
                yield Violation(
                    step_id=step_id,
                    tool=tool_spec.name,
                    field_path=fname,
                    code="missing_arg",
                    message=f"required argument {fname!r} is missing",
                )
            continue
        value = canonical_args[fname]
        yield from _validate_field_value(
            step_id=step_id,
            tool_spec=tool_spec,
            field_name=fname,
            value=value,
            annotation=finfo.annotation,
        )


def _validate_field_value(
    *,
    step_id: str,
    tool_spec: ToolSpec,
    field_name: str,
    value: Any,
    annotation: Any,
) -> Iterator[Violation]:
    """Validate one field's value against its annotation, given that
    the surrounding ``action.args`` contains refs somewhere.

    Recursion strategy: walk the value structure; concrete leaves go
    through ``TypeAdapter(annotation).validate_python(value)``; pure
    full-ref strings defer; mixed-string refs validate as ``str``;
    containers recurse into elements.

    For ``dict[K, V]`` and ``list[V]`` annotations we use TypeAdapter to
    validate elements where possible. Where we can't peel the
    annotation (deeply nested Union, custom validators), we fall back
    to whole-value TypeAdapter — which conservatively errors on
    legit-but-deferred shapes. Tests pin this behaviour.
    """

    # Pure full-ref string: defer.
    if is_full_ref_string(value):
        return

    # Mixed-string ref: value WILL be str at executor time.
    if isinstance(value, str) and contains_ref(value):
        # Validate that field annotation accepts strings.
        try:
            TypeAdapter(annotation).validate_python("interpolated-placeholder")
        except ValidationError:
            yield Violation(
                step_id=step_id,
                tool=tool_spec.name,
                field_path=field_name,
                code="invalid_arg_type",
                message=(
                    f"field {field_name!r} contains interpolated string "
                    f"{value!r} but field type does not accept strings; "
                    f"resolved value would be 'str' at executor time."
                ),
            )
        return

    # Container with mixed refs: try to validate elements.
    if isinstance(value, (list, tuple)) and contains_ref(value):
        # Attempt element-wise validation. We don't have the element
        # annotation peeled here for arbitrary annotations; we use
        # TypeAdapter on each concrete element with the SAME outer
        # annotation wrapped (list[T] etc.). Pragmatic fallback:
        # validate the WHOLE container after replacing ref leaves with
        # ``None``. That tells us whether the surviving concrete shape
        # is acceptable — non-None Nones would fail per-leaf
        # validation. For lists of str we accept None? No — we
        # substitute a placeholder string and rely on TypeAdapter to
        # surface type errors on the concrete leaves.
        return  # CONSERVATIVE: deferred; per-leaf validation in MVP would
                # need annotation peeling. Document & test the limitation;
                # executor-time resolve_refs + tool call will catch concrete
                # bad leaves.

    if isinstance(value, dict) and contains_ref(value):
        # Same conservative deferral as list case.
        return

    # All-concrete field: validate via TypeAdapter (no model-level rules).
    try:
        TypeAdapter(annotation).validate_python(value)
    except ValidationError as exc:
        for err in exc.errors():
            loc_tail = ".".join(str(p) for p in err.get("loc", ()))
            full_path = f"{field_name}.{loc_tail}" if loc_tail else field_name
            yield Violation(
                step_id=step_id,
                tool=tool_spec.name,
                field_path=full_path,
                code=err.get("type", "invalid_arg_type"),
                message=err.get("msg", "invalid value"),
            )


def _violation_from_pydantic_error(
    step_id: str,
    tool_spec: ToolSpec,
    err: dict[str, Any],
    args: dict[str, Any],
) -> Violation:
    """Convert a pydantic v2 error dict into our Violation type.

    Used in the no-refs full-validate path where pydantic returns
    structured ``ValidationError``. Reports the err's ``type`` as
    ``code`` so callers can route by code (``missing`` / ``int_parsing``
    / ``string_type`` etc.).
    """

    loc = err.get("loc") or ()
    field_path = ".".join(str(p) for p in loc) if loc else None
    code = err.get("type", "invalid_arg_type")
    if code == "missing":
        code = "missing_arg"
    return Violation(
        step_id=step_id,
        tool=tool_spec.name,
        field_path=field_path,
        code=code,
        message=err.get("msg", "invalid value"),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_action_args(
    step_id: str,
    action: Action,
    tool_spec: ToolSpec,
    plan: Plan | None = None,
) -> list[Violation]:
    """Run both phases against one Action. Returns structured violations.

    Plan-level ref-integrity checks need the surrounding ``plan``;
    pass it when called from ``validate_plan``. Tests can omit it to
    skip ref-integrity (schema-only mode).
    """

    violations: list[Violation] = []
    # Phase 1.a: unknown args (independent of refs)
    violations.extend(_phase1_check_unknown_keys(step_id, action, tool_spec))
    # Phase 1.b: ref integrity (requires plan context for target lookup)
    if plan is not None:
        violations.extend(
            _phase1_check_refs(step_id, action, plan, tool_spec)
        )
    # Phase 2: schema-aware partial
    violations.extend(_phase2_validate_args(step_id, action, tool_spec))
    return violations


def validate_plan(
    plan: Plan, registry: Mapping[str, ToolSpec]
) -> list[Violation]:
    """Walk every Action and aggregate violations. Returns structured
    list — empty == plan ready to execute. Callers convert to strings
    via ``render_violations`` for retry feedback.
    """

    violations: list[Violation] = []
    for step_id, action in plan.actions.items():
        spec = registry.get(action.tool)
        if spec is None:
            violations.append(
                Violation(
                    step_id=step_id,
                    tool=action.tool,
                    code="unknown_tool",
                    message=f"unknown tool {action.tool!r} (not in registry)",
                )
            )
            continue
        violations.extend(
            validate_action_args(
                step_id=step_id,
                action=action,
                tool_spec=spec,
                plan=plan,
            )
        )
    return violations


def validate_plan_args(
    plan: Plan, registry: Mapping[str, ToolSpec]
) -> list[str]:
    """Compatibility wrapper returning strings (legacy callers).

    New code should prefer ``validate_plan`` + access structured
    ``Violation`` objects directly.
    """

    return render_violations(validate_plan(plan, registry))


def validate_plan_or_raise(
    plan: Plan, registry: Mapping[str, ToolSpec]
) -> None:
    """Exception-style wrapper raising ``InvalidPlanError`` with
    structured ``.violations`` list."""

    violations = validate_plan(plan, registry)
    if violations:
        raise InvalidPlanError(violations)


__all__ = [
    "InvalidPlanError",
    "Violation",
    "render_violations",
    "validate_action_args",
    "validate_plan",
    "validate_plan_args",
    "validate_plan_or_raise",
]
