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

import types
import typing
from typing import Annotated, Any, Iterator, Mapping, Union, get_args, get_origin

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

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


def _model_populates_by_name(model: type[BaseModel]) -> bool:
    """Read ``populate_by_name`` from the model config — pydantic v2
    accepts field names alongside aliases ONLY when this is True.
    Default False matches pydantic's default.
    """

    cfg = getattr(model, "model_config", None)
    if cfg is None:
        return False
    if isinstance(cfg, dict):
        return bool(cfg.get("populate_by_name", False))
    # ConfigDict subclass instance (rare path)
    return bool(getattr(cfg, "populate_by_name", False))


def _iter_validation_alias_keys(validation_alias: object) -> Iterator[str]:
    """Yield every accepted input key from a pydantic v2
    ``validation_alias`` value. Handles ``str`` / ``AliasChoices`` /
    ``AliasPath`` (Codex R2 MAJOR #3).

    For ``AliasPath``, only the first segment is keyable at the top
    level — sub-segments would need walking into nested dicts which is
    out of scope for arg validation in this MVP.
    """

    if validation_alias is None:
        return
    if isinstance(validation_alias, str):
        yield validation_alias
        return
    if isinstance(validation_alias, AliasChoices):
        for choice in validation_alias.choices:
            yield from _iter_validation_alias_keys(choice)
        return
    if isinstance(validation_alias, AliasPath):
        first = validation_alias.path[0] if validation_alias.path else None
        if isinstance(first, str):
            yield first
        return


def _accepted_field_names(model: type[BaseModel]) -> set[str]:
    """Set of input keys the planner may legitimately use for this
    model. Mirrors pydantic semantics exactly:

    - If ``populate_by_name=True``: field names AND aliases accepted.
    - If False (pydantic default): when ``alias`` / ``validation_alias``
      is set, accept ONLY that; otherwise accept the field name.

    Closes Codex R2 MAJOR #3 — was previously over-permissive.
    """

    by_name = _model_populates_by_name(model)
    names: set[str] = set()
    for fname, finfo in model.model_fields.items():
        alias = getattr(finfo, "alias", None)
        validation_alias = getattr(finfo, "validation_alias", None)
        has_alias = alias is not None or validation_alias is not None
        if alias:
            names.add(alias)
        for key in _iter_validation_alias_keys(validation_alias):
            names.add(key)
        if by_name or not has_alias:
            names.add(fname)
    return names


def _normalize_to_field_name(
    key: str, model: type[BaseModel]
) -> str | None:
    """Map a planner-emitted input key (alias or field name) back to
    the canonical field name in ``model.model_fields``.

    Respects ``populate_by_name`` config so the per-field walk uses
    the same acceptance rule as ``_accepted_field_names`` — no
    over-permissive normalisation.
    """

    by_name = _model_populates_by_name(model)
    for fname, finfo in model.model_fields.items():
        alias = getattr(finfo, "alias", None)
        validation_alias = getattr(finfo, "validation_alias", None)
        has_alias = alias is not None or validation_alias is not None
        if alias == key:
            return fname
        if any(k == key for k in _iter_validation_alias_keys(validation_alias)):
            return fname
        if key == fname and (by_name or not has_alias):
            return fname
    return None


# ---------------------------------------------------------------------------
# Annotation helpers (Codex R2 MAJOR #2 + #1)
# ---------------------------------------------------------------------------


def _annotation_with_constraints(finfo: Any) -> Any:
    """Build a TypeAdapter-friendly annotation that PRESERVES
    pydantic Field constraints (``ge``, ``le``, ``max_length``, custom
    validators in ``finfo.metadata``).

    Plain ``TypeAdapter(finfo.annotation)`` drops these — closing
    Codex R2 MAJOR #2.
    """

    annotation = finfo.annotation
    metadata = getattr(finfo, "metadata", None) or []
    if not metadata:
        return annotation
    return Annotated[(annotation, *metadata)]


def _annotation_accepts_string(annotation: Any) -> bool:
    """Return True if a value of type ``str`` could be accepted by
    ``annotation`` (a typing hint or pydantic-compatible type).

    Walks Optional / Union / Annotated wrappers. Conservative: returns
    True for ``Any`` and unknown types so we don't over-reject mixed
    interpolated strings against custom string-like fields
    (Codex R2 MINOR #5).
    """

    if annotation is None:
        return False
    if annotation is Any:
        return True
    if annotation is str:
        return True

    origin = get_origin(annotation)
    if origin is Annotated:
        inner = get_args(annotation)[0]
        return _annotation_accepts_string(inner)
    if origin is Union or origin is types.UnionType:
        return any(_annotation_accepts_string(arg) for arg in get_args(annotation))
    # str subclass / Literal[str_value, ...] / typing.NewType / etc.
    if isinstance(annotation, type) and issubclass(annotation, str):
        return True
    if origin is typing.Literal:
        return all(isinstance(arg, str) for arg in get_args(annotation))
    return False


def _peel_container_element_annotation(annotation: Any) -> Any | None:
    """For ``list[T]`` / ``tuple[T, ...]`` / ``set[T]`` return the
    element annotation ``T``. For ``dict[K, V]`` return the value
    annotation ``V``. Return None when the container shape is not
    statically peelable (e.g. ``list`` without type param, custom
    classes). Closes Codex R2 MAJOR #1 — element-walk for mixed-ref
    containers.
    """

    origin = get_origin(annotation)
    # Strip Annotated wrapper.
    if origin is Annotated:
        return _peel_container_element_annotation(get_args(annotation)[0])
    if origin in (list, set, frozenset):
        args = get_args(annotation)
        return args[0] if args else None
    if origin is tuple:
        args = get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            return args[0]  # tuple[T, ...]
        # Heterogeneous tuple — element-walk isn't homogeneous; caller
        # falls back to whole-value defer.
        return None
    if origin is dict:
        args = get_args(annotation)
        return args[1] if len(args) >= 2 else None
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


# ---------------------------------------------------------------------------
# Ref-dependency graph + cycle detection (Codex R2 MAJOR #4)
# ---------------------------------------------------------------------------


def _build_dep_graph(plan: Plan) -> dict[str, set[str]]:
    """Return adjacency ``consumer → {producer_step_ids}`` combining
    ref-derived edges (from ``iter_refs(action.args)``) and explicit
    ``depends_on`` edges.

    Self-refs and edges to non-existent steps are excluded — they're
    already reported as per-action violations and shouldn't poison
    the cycle search.

    Used by ``_phase1_detect_ref_cycles`` to surface cycles that
    Plan-schema's depends_on cycle detection misses (refs in args
    aren't visible to it).
    """

    graph: dict[str, set[str]] = {}
    for step_id, action in plan.actions.items():
        producers: set[str] = set()
        for ref_path in iter_refs(action.args):
            target = extract_step_id(ref_path)
            if target != step_id and target in plan.actions:
                producers.add(target)
        for dep in action.depends_on:
            if dep != step_id and dep in plan.actions:
                producers.add(dep)
        graph[step_id] = producers
    return graph


def _phase1_detect_ref_cycles(plan: Plan) -> Iterator[Violation]:
    """Detect cycles formed by ref-derived edges (which Plan schema
    cannot see — it only walks ``depends_on``).

    Emits one ``cycle`` Violation per step participating in any cycle
    so the planner-retry feedback can name each offender.
    """

    graph = _build_dep_graph(plan)
    cycle_nodes: set[str] = set()
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {step_id: WHITE for step_id in graph}

    def _dfs(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for nbr in graph.get(node, ()):
            if color.get(nbr, WHITE) == GRAY:
                # Back edge — every node from `stack[stack.index(nbr):]`
                # up to current is in the cycle.
                cycle_nodes.update(stack[stack.index(nbr):])
            elif color.get(nbr, WHITE) == WHITE:
                _dfs(nbr, stack)
        stack.pop()
        color[node] = BLACK

    for step_id in graph:
        if color[step_id] == WHITE:
            _dfs(step_id, [])

    for step_id in sorted(cycle_nodes):
        yield Violation(
            step_id=step_id,
            tool=plan.actions[step_id].tool,
            code="cycle",
            message=(
                f"step {step_id!r} participates in a reference cycle. "
                f"The plan has a circular dependency formed via "
                f"``${{...}}`` refs and/or ``depends_on`` — executor "
                f"cannot order it. Cycle members: {sorted(cycle_nodes)}."
            ),
        )


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
        # Annotation WITH Field(...) constraints (Codex R2 MAJOR #2).
        annotation_with_constraints = _annotation_with_constraints(finfo)
        yield from _validate_field_value(
            step_id=step_id,
            tool_spec=tool_spec,
            field_name=fname,
            value=value,
            annotation=annotation_with_constraints,
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

    Strategy:
    - Pure full-ref string (``"${s1.x}"``): defer — type resolves at
      executor time from the producer's output.
    - Mixed interpolated string (``"prefix ${s1.x}"``): the resolved
      value will be ``str``; reject if the annotation can't accept str
      (Codex R2 MINOR #5 — switched to annotation introspection
      instead of TypeAdapter-with-placeholder which over-rejected
      constrained-string custom types).
    - Container with refs: peel the element annotation via
      ``get_origin/get_args`` and validate every concrete leaf
      (Codex R2 MAJOR #1). Ref leaves defer. If annotation can't be
      peeled (custom class, missing type params), fall back to defer.
    - All-concrete leaf or container: full ``TypeAdapter`` with
      annotation that PRESERVES ``Field(...)`` constraints
      (Codex R2 MAJOR #2 — pulled in by caller).
    """

    if is_full_ref_string(value):
        return

    if isinstance(value, str) and contains_ref(value):
        if not _annotation_accepts_string(annotation):
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

    if isinstance(value, (list, tuple, set, frozenset)) and contains_ref(value):
        element_annotation = _peel_container_element_annotation(annotation)
        if element_annotation is None:
            return  # Can't peel — defer.
        for idx, item in enumerate(value):
            if contains_ref(item):
                continue
            yield from _validate_field_value(
                step_id=step_id,
                tool_spec=tool_spec,
                field_name=f"{field_name}[{idx}]",
                value=item,
                annotation=element_annotation,
            )
        return

    if isinstance(value, dict) and contains_ref(value):
        element_annotation = _peel_container_element_annotation(annotation)
        if element_annotation is None:
            return  # Can't peel — defer.
        for k, item in value.items():
            if contains_ref(item):
                continue
            yield from _validate_field_value(
                step_id=step_id,
                tool_spec=tool_spec,
                field_name=f"{field_name}[{k!r}]",
                value=item,
                annotation=element_annotation,
            )
        return

    # All-concrete value: full TypeAdapter check (annotation already
    # carries Field constraints from caller).
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
    # Cross-action cycle check via ref edges + depends_on
    # (Codex R2 MAJOR #4 — Plan schema only sees depends_on edges).
    violations.extend(_phase1_detect_ref_cycles(plan))
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
