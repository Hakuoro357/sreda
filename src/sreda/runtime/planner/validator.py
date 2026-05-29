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

Known deferred limitations:

1. Producer/consumer ref-type match (Codex R1 MAJOR #6):
   We do NOT statically check that ``${s1.items}`` into
   ``items: list[str]`` matches producer ``output_model.items:
   list[str]``. Walking the producer's output union by ref-path is
   beyond MVP scope. Executor-time validation catches the resolved-value
   mismatch when the tool actually receives the args.

2. ``@field_validator`` decorators (Codex R5 MAJOR #2):
   When ANY ref is present in ``action.args``, per-field validation
   uses ``TypeAdapter(annotation)`` which preserves ``Annotated[...]``
   constraints but NOT ``@field_validator`` decorators attached to the
   model. A concrete bad value in a field with ``@field_validator``
   passes plan validation if a sibling field is a ref. **Mitigation**:
   Sub-A4 ``ToolSpec.input_model`` definitions SHOULD prefer
   ``Field(...)`` constraints (``ge``, ``max_length``, ``pattern``,
   ``Annotated[T, ...]``) over ``@field_validator`` for single-field
   shape rules — those flow through TypeAdapter. Cross-field
   ``@model_validator`` is harder to handle correctly under refs and
   is documented-deferred regardless. Tested via
   ``test_model_validator_does_not_fire_when_refs_present``.

3. Fixed-length heterogeneous tuple fields (Codex R5 MINOR #3):
   ``tuple[int, str]`` with a ref in one position: concrete sibling
   positions are NOT statically validated, and arity is not checked
   (because ``_check_container_shell_constraints`` builds shell as
   ``tuple[Any, ...]``). In practice planner-emitted JSON has lists,
   not tuples — no ToolSpec input model is expected to use fixed
   tuple fields. Accepted limitation.
"""

from __future__ import annotations

import collections.abc
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
    iter_malformed_ref_tokens,
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


def _field_validation_keys(finfo: Any) -> list[str]:
    """Return the keys that pydantic v2 ``model_validate`` accepts as
    INPUT for this field, in precedence order (Codex R4 MAJOR #3):

    1. If ``validation_alias`` is set → use ONLY that (alias is for
       SERIALIZATION when alias-only; not consulted as input here).
    2. Else if ``alias`` is set → that's the validation input key.
    3. Else → no alias; ``populate_by_name`` doesn't change this.

    Field name itself is added separately based on ``populate_by_name``
    and the «no-alias-set» case (handled by ``_accepted_field_names``).
    """

    validation_alias = getattr(finfo, "validation_alias", None)
    if validation_alias is not None:
        return list(_iter_validation_alias_keys(validation_alias))
    alias = getattr(finfo, "alias", None)
    if alias is not None:
        return [alias]
    return []


def _accepted_field_names(model: type[BaseModel]) -> set[str]:
    """Set of input keys the planner may legitimately use for this
    model. Mirrors pydantic v2 input-validation semantics:

    - If ``validation_alias`` is set on a field → accept only that
      (alias used purely for serialization).
    - Else if ``alias`` is set → accept the alias.
    - Else (no alias declared) → accept the field name.
    - Plus: if ``populate_by_name=True`` is configured, the field name
      is ALSO accepted alongside the alias/validation_alias.

    Codex R4 MAJOR #3 closes the divergence where validation_alias and
    alias were both accepted when only validation_alias should be.
    """

    by_name = _model_populates_by_name(model)
    names: set[str] = set()
    for fname, finfo in model.model_fields.items():
        validation_keys = _field_validation_keys(finfo)
        if validation_keys:
            names.update(validation_keys)
            if by_name:
                names.add(fname)
        else:
            # No alias declared — field name is the canonical input.
            names.add(fname)
    return names


def _normalize_to_field_name(
    key: str, model: type[BaseModel]
) -> str | None:
    """Map a planner-emitted input key back to the canonical field name
    in ``model.model_fields``. Same precedence rules as
    ``_accepted_field_names`` so coverage stays in lock-step.
    """

    by_name = _model_populates_by_name(model)
    for fname, finfo in model.model_fields.items():
        validation_keys = _field_validation_keys(finfo)
        if validation_keys:
            if key in validation_keys:
                return fname
            if by_name and key == fname:
                return fname
        else:
            if key == fname:
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


_DEFINITELY_NOT_STRING: tuple[type, ...] = (
    int, float, bool, bytes, list, tuple, set, frozenset, dict,
)


def _annotation_accepts_string(annotation: Any) -> bool:
    """Return True if a value of type ``str`` could plausibly be
    accepted by ``annotation``.

    Conservative semantics (Codex R3 MINOR #6): we ONLY return False
    when we can prove the annotation cannot accept a string —
    primitive non-str types (int/float/bool/bytes) and built-in
    containers. NewType, pydantic-validated strings, custom validators,
    and other unknown types pass through to executor-time validation
    (the eventual tool call will see the resolved string and decide).
    """

    if annotation is None or annotation is type(None):
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
        return any(
            _annotation_accepts_string(arg) for arg in get_args(annotation)
        )
    if origin is typing.Literal:
        return all(isinstance(arg, str) for arg in get_args(annotation))

    # Concrete classes: str / str-subclass → True; primitives + builtin
    # containers → False; everything else (NewType, custom validators,
    # pydantic types, etc.) → True (conservative defer).
    if isinstance(annotation, type):
        if issubclass(annotation, str):
            return True
        if issubclass(annotation, _DEFINITELY_NOT_STRING):
            return False
        return True  # unknown class — defer to executor

    # Container origin (list/dict/etc.) — concrete non-string.
    if origin in _DEFINITELY_NOT_STRING:
        return False

    # Anything else (NewType, ParamSpec, etc.) — defer.
    return True


def _strip_optional_and_annotated(annotation: Any) -> Any:
    """Unwrap ``Annotated[T, ...]`` and ``T | None`` / ``Optional[T]``
    layers, returning the innermost non-Optional type.

    For multi-arm unions like ``int | str | None``, returns the first
    non-None arm; this is a pragmatic choice — real planner ToolSpec
    inputs rarely have unions other than Optional, and the executor
    will resolve refs and try the actual value against the original
    annotation eventually.
    """

    origin = get_origin(annotation)
    if origin is Annotated:
        return _strip_optional_and_annotated(get_args(annotation)[0])
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return _strip_optional_and_annotated(non_none[0])
        # Multi-arm: return the first non-None for container/dict probes;
        # callers that need full union semantics handle it themselves.
        if non_none:
            return _strip_optional_and_annotated(non_none[0])
    return annotation


def _peel_container_element_annotation(
    annotation: Any,
) -> Any | None:
    """For ``list[T]`` / ``tuple[T, ...]`` / ``set[T]`` / ``frozenset[T]``
    return element annotation ``T``. For ``dict[K, V]`` return ``V``.
    For non-peelable shapes return None.

    Strips ``Annotated`` and ``Optional`` wrappers so
    ``Optional[list[str]]`` and ``Annotated[list[str], Field(min_length=2)]``
    peel to ``str`` (Codex R3 MAJOR #1).
    """

    bare = _strip_optional_and_annotated(annotation)
    origin = get_origin(bare)
    if origin in (list, set, frozenset):
        args = get_args(bare)
        return args[0] if args else None
    if origin is tuple:
        args = get_args(bare)
        if len(args) == 2 and args[1] is Ellipsis:
            return args[0]
        return None
    if origin is dict:
        args = get_args(bare)
        return args[1] if len(args) >= 2 else None
    return None


def _peel_dict_key_annotation(annotation: Any) -> Any | None:
    """Return the key annotation ``K`` for ``dict[K, V]`` (Codex R3
    MINOR #7). None for non-dict or untyped dict."""

    bare = _strip_optional_and_annotated(annotation)
    if get_origin(bare) is dict:
        args = get_args(bare)
        return args[0] if args else None
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


def _iter_dict_keys_with_refs(value: Any) -> Iterator[str]:
    """Recursively yield every string DICT KEY (any nesting depth) in
    ``value`` that contains a ``${...}`` ref. Existing ``contains_ref``
    / ``iter_refs`` only walk dict values; refs in keys need this
    separate walk to surface as ``unsupported_ref_location``.
    Codex R4 MINOR #4.
    """

    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and contains_ref(k):
                yield k
            yield from _iter_dict_keys_with_refs(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_dict_keys_with_refs(item)


def _phase1_check_ref_in_dict_keys(
    step_id: str, action: Action, tool_spec: ToolSpec | None
) -> Iterator[Violation]:
    """Emit ``unsupported_ref_location`` violations for ``${...}``
    references that appear as DICT KEYS anywhere in ``action.args``.
    Executor doesn't resolve dict keys; surfacing this at plan time
    prevents silent planner typos.
    """

    for ref_key in _iter_dict_keys_with_refs(action.args):
        yield Violation(
            step_id=step_id,
            tool=tool_spec.name if tool_spec else None,
            field_path=f"<dict key {ref_key!r}>",
            code="unsupported_ref_location",
            message=(
                f"reference {ref_key!r} appears as a DICT KEY in "
                f"action args. References in dict keys are not "
                f"resolved by executor — refactor to use the ref as a "
                f"dict VALUE or top-level field."
            ),
        )


def _phase1_check_ref_syntax(
    step_id: str,
    action: Action,
    tool_spec: ToolSpec | None,
) -> Iterator[Violation]:
    """Yield ``invalid_ref_syntax`` violations for any ``${...}`` token
    in ``action.args`` that doesn't match the strict ref grammar OR
    contains underscore-prefixed (private) segments.

    Codex R3 MINOR #5: malformed tokens like ``${s1.}``, ``${s1..x}``,
    ``${s1._private}`` previously passed validator and either survived
    into the tool as literal text or crashed executor's
    ``resolve_refs``. This check catches them at plan time.
    """

    for malformed in iter_malformed_ref_tokens(action.args):
        yield Violation(
            step_id=step_id,
            tool=tool_spec.name if tool_spec else None,
            field_path=None,
            code="invalid_ref_syntax",
            message=(
                f"malformed reference {malformed!r}. Refs must be of "
                f"the form ``${{step_id.field.subfield}}`` — segments "
                f"non-empty, no underscore-prefixed segments, no "
                f"consecutive or trailing dots."
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


def build_full_dep_graph(plan: Plan) -> dict[str, set[str]]:
    """Codex Sub-A12 B.3 R2 MAJOR (HIGH): single source of truth for the
    plan's dependency graph. Used by:

    1. ``_phase1_detect_ref_cycles`` — cycle detection at validate_plan
       time, so cycles via ``next`` edges or branch compose refs surface
       as validator violations (not later as compiler PlanCompileError).
    2. ``plan_compiler.compile`` — topological layer building.

    Edge sources:
    - ``action.args`` refs (``${stepN.field}``)
    - ``action.depends_on`` explicit deps
    - ``expected_outcomes[].next`` control-flow edges
    - ``expected_outcomes[].compose.template_data`` refs (branch compose)

    Excludes self-refs and edges to non-existent steps (those are
    reported separately by other phases; including them here would
    poison cycle detection)."""
    graph = _build_args_dep_graph(plan)
    for step_id, action in plan.actions.items():
        for branch in action.expected_outcomes:
            # next-edges: s1.next="s2" → s2 depends on s1
            if branch.next and branch.next != step_id and branch.next in plan.actions:
                graph.setdefault(branch.next, set()).add(step_id)
            # branch compose ref-edges: host depends on referenced steps
            if branch.compose is None:
                continue
            for ref_path in iter_refs(branch.compose.template_data):
                target = extract_step_id(ref_path)
                if target != step_id and target in plan.actions:
                    graph.setdefault(step_id, set()).add(target)
    return graph


def _build_dep_graph(plan: Plan) -> dict[str, set[str]]:
    """Legacy alias — kept for backwards compat with plan_compiler.

    Codex Sub-A12 B.3 R2: returns the FULL graph (args + depends_on +
    next + branch compose refs) so validator cycle detection and
    compiler topo sort see the same edges. Earlier this only had
    args + depends_on, which let cycles via next/compose slip past
    validate_plan and surface later as compiler PlanCompileError."""
    return build_full_dep_graph(plan)


def _build_args_dep_graph(plan: Plan) -> dict[str, set[str]]:
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


def _phase1_check_required_any_non_null(
    step_id: str, action: Action, tool_spec: ToolSpec
) -> Iterator[Violation]:
    """Codex Sub-A4 R5/R6 MAJOR #1 — enforce ``ToolSpec.required_any_non_null_args``.

    Some tools (notably ``update_shopping_item``) have an input shape
    where every mutable field is individually optional but the call is
    a silent no-op if all of them are absent or explicit-null. The
    ``input_model``'s ``@model_validator`` catches this for fully-
    literal args, but Phase 2's refs-present path uses per-field
    ``TypeAdapter`` checks which skip model_validators. So a plan like
    ``{"item_id": "${s1.items[0].item_id}"}`` with no mutable fields
    passes Phase 2 unchecked and ships a no-op tool call.

    This phase 1 check fires INDEPENDENT of refs — it operates on the
    raw ``action.args`` keys and values. Rules:

    - A field counts as «provided» if its key is in ``action.args``
      AND its value is **not Python ``None``**.
    - Refs (``${...}``) count as non-null-by-shape (planner trusts
      upstream output_model shapes; if the resolved value is ``None``
      at execute time, the executor's
      ``spec.validate_args_at_execute_time(resolved)`` catches it via
      full ``input_model.model_validate``).
    - Empty strings count as non-null (e.g. shopping
      ``quantity_text=""`` is the legitimate clear-quantity intent).
    - Explicit ``null`` (Python ``None``) and missing keys do NOT
      count as provided — both are no-op contributions at runtime.

    No-op when ``required_any_non_null_args`` is unset (most tools).
    """

    if tool_spec.required_any_non_null_args is None:
        return
    provided = [
        name
        for name in tool_spec.required_any_non_null_args
        if name in action.args and action.args[name] is not None
    ]
    if provided:
        return
    yield Violation(
        step_id=step_id,
        tool=tool_spec.name,
        code="silent_noop_call",
        message=(
            f"call must provide at least one of "
            f"{tool_spec.required_any_non_null_args} with a non-null "
            f"value (refs and empty strings count as non-null; "
            f"explicit null and missing keys do not). Got args with "
            f"keys {sorted(action.args.keys())} — this would be a "
            f"silent no-op call at runtime."
        ),
    )


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

    # Use shared normaliser (Codex R5 MAJOR #1) so top-level and nested
    # paths share alias/duplicate semantics.
    canonical_args, duplicate_violations = _normalize_model_input(
        step_id=step_id,
        tool_name=tool_spec.name,
        field_path_prefix=None,
        model=model,
        args=action.args,
    )
    yield from duplicate_violations
    # Original-key dict for full no-refs model_validate (uses pydantic's
    # own alias resolution semantics — see also test
    # ``test_arg_under_alias_is_accepted``).
    known_args_as_emitted = {
        k: v for k, v in action.args.items() if k in accepted_names
    }

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


def _normalize_model_input(
    *,
    step_id: str,
    tool_name: str,
    field_path_prefix: str | None,
    model: type[BaseModel],
    args: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Violation]]:
    """Shared model-input normaliser used by both top-level Phase 2 and
    nested-BaseModel recursion (Codex R5 MAJOR #1, R6 MINOR #2 — split
    tool_name and field_path_prefix so violations have clean paths).

    Returns ``(canonical_args, violations)``:
    - ``canonical_args``: keys remapped to canonical field names. Keys
      not in ``model.model_fields`` are dropped (Phase 1 reports them).
    - ``violations``: list of ``duplicate_arg`` Violations for fields
      that received >1 emitted key.

    Args:
        tool_name: actual tool name for the Violation's ``tool`` field.
            Same at all levels — refers to the OUTER plan action's tool.
        field_path_prefix: ``None`` for top-level (path = canonical_key);
            ``"author"`` for nested (path = ``"author.canonical_key"``).
            Renderer prints ``tool`` and ``field_path`` separately, so
            ``field_path`` must be the JSON-path inside args, NOT a
            redundant ``tool:path`` string.
    """

    accepted_names = _accepted_field_names(model)
    canonical_args: dict[str, Any] = {}
    canonical_emitted_keys: dict[str, list[str]] = {}
    for key, value in args.items():
        if key not in accepted_names:
            continue
        canonical_key = _normalize_to_field_name(key, model)
        if canonical_key is not None:
            canonical_args[canonical_key] = value
            canonical_emitted_keys.setdefault(canonical_key, []).append(key)

    violations: list[Violation] = []
    for canonical_key, emitted_keys in canonical_emitted_keys.items():
        if len(emitted_keys) > 1:
            field_path = (
                f"{field_path_prefix}.{canonical_key}"
                if field_path_prefix
                else canonical_key
            )
            violations.append(Violation(
                step_id=step_id,
                tool=tool_name,
                field_path=field_path,
                code="duplicate_arg",
                message=(
                    f"field {canonical_key!r} supplied under multiple "
                    f"names: {sorted(emitted_keys)}. Pick the alias or "
                    f"the field name, not both."
                ),
            ))
    return canonical_args, violations


def _try_extract_basemodel(annotation: Any) -> type[BaseModel] | None:
    """If ``annotation`` is (possibly wrapped in Optional/Annotated) a
    pydantic ``BaseModel`` subclass, return that subclass. Otherwise
    None. Codex R4 MAJOR #1 — used to detect nested-model fields.
    """

    bare = _strip_optional_and_annotated(annotation)
    if isinstance(bare, type) and issubclass(bare, BaseModel):
        return bare
    return None


def _check_container_shell_constraints(
    *,
    step_id: str,
    tool_spec: ToolSpec,
    field_name: str,
    value: Any,
    annotation: Any,
) -> Iterator[Violation]:
    """Check container-level length constraints that are statically
    knowable even when the container has ref leaves (Codex R4 MAJOR #2).

    Refs substitute in-place inside containers (a ref leaf becomes one
    element, not a splice), so ``len(literal_container)`` is the final
    length. ``Field(min_length=2)`` on ``["${s1.x}"]`` is a static
    violation regardless of what s1.x resolves to.

    Builds an `Annotated[list[Any], *length_metadata]` shell and runs
    TypeAdapter — extracts ``MinLen``/``MaxLen`` from pydantic
    metadata. Only checks length-style constraints (not element type),
    so element walking still has work to do.
    """

    # Find length-only metadata from the original Annotated wrapper.
    origin = get_origin(annotation)
    if origin is not Annotated:
        return  # No constraints attached at this level.
    args = get_args(annotation)
    # args[0] is the inner type, args[1:] are metadata objects.
    metadata = args[1:]
    if not metadata:
        return
    # Filter to length-style constraints only — using annotated_types
    # MinLen/MaxLen if present, plus a generic name-check fallback so
    # we don't depend on the import.
    length_metadata = [
        m for m in metadata
        if type(m).__name__ in {"MinLen", "MaxLen", "Len"}
    ]
    if not length_metadata:
        return
    # Build a shell type that matches the value's actual container
    # type with Any elements. ``list``/``dict``/``set``/``frozenset``/
    # ``tuple`` covered; for unknown container types we skip.
    if isinstance(value, list):
        shell = list[Any]
    elif isinstance(value, tuple):
        shell = tuple[Any, ...]
    elif isinstance(value, set):
        shell = set[Any]
    elif isinstance(value, frozenset):
        shell = frozenset[Any]
    elif isinstance(value, dict):
        shell = dict[Any, Any]
    else:
        return
    shell_annotation = Annotated[(shell, *length_metadata)]
    try:
        TypeAdapter(shell_annotation).validate_python(value)
    except ValidationError as exc:
        for err in exc.errors():
            yield Violation(
                step_id=step_id,
                tool=tool_spec.name,
                field_path=field_name,
                code=err.get("type", "invalid_arg_type"),
                message=err.get("msg", "container constraint violated"),
            )


def _validate_nested_basemodel_partial(
    *,
    step_id: str,
    tool_spec: ToolSpec,
    field_name: str,
    value: dict[str, Any],
    nested_model: type[BaseModel],
) -> Iterator[Violation]:
    """Recursive partial validation for nested ``BaseModel`` fields.

    Mirrors ``_phase2_validate_args`` logic at the nested level:
    - Check unknown keys against ``nested_model.model_fields``.
    - When refs present, do per-field validation respecting
      pydantic input-key rules + Field constraints.
    - When no refs in the nested dict, full ``nested_model.model_validate``.

    Field path is prefixed with the parent field name (e.g.
    ``payload.author.name``) so retry feedback locates errors precisely.
    Codex R4 MAJOR #1.
    """

    accepted_names = _accepted_field_names(nested_model)
    # Phase 1 within the nested model: unknown keys
    for key in value.keys():
        if key not in accepted_names:
            yield Violation(
                step_id=step_id,
                tool=tool_spec.name,
                field_path=f"{field_name}.{key}",
                code="unknown_arg",
                message=(
                    f"nested model {nested_model.__name__} got unknown "
                    f"key {key!r}. Accepted: {sorted(accepted_names)}."
                ),
            )

    # Shared canonicalisation + duplicate detection (Codex R5 MAJOR #1,
    # R6 MINOR #2 — clean field_path without tool name redundancy).
    canonical_value, duplicate_violations = _normalize_model_input(
        step_id=step_id,
        tool_name=tool_spec.name,
        field_path_prefix=field_name,
        model=nested_model,
        args=value,
    )
    yield from duplicate_violations

    if not contains_ref(value):
        # No refs at this level — full nested model_validate using
        # keys as the planner emitted them (so pydantic's own alias
        # rules apply).
        try:
            nested_model.model_validate(
                {k: v for k, v in value.items() if k in accepted_names}
            )
        except ValidationError as exc:
            for err in exc.errors():
                loc = err.get("loc") or ()
                loc_tail = ".".join(str(p) for p in loc) if loc else ""
                full_path = (
                    f"{field_name}.{loc_tail}" if loc_tail else field_name
                )
                code = err.get("type", "invalid_arg_type")
                if code == "missing":
                    code = "missing_arg"
                yield Violation(
                    step_id=step_id,
                    tool=tool_spec.name,
                    field_path=full_path,
                    code=code,
                    message=err.get("msg", "invalid value"),
                )
        return

    # Refs present — per-field walk with constraints preserved.
    for sub_fname, sub_finfo in nested_model.model_fields.items():
        if sub_fname not in canonical_value:
            try:
                required = sub_finfo.is_required()
            except AttributeError:  # pragma: no cover
                required = sub_finfo.default is ...
            if required:
                yield Violation(
                    step_id=step_id,
                    tool=tool_spec.name,
                    field_path=f"{field_name}.{sub_fname}",
                    code="missing_arg",
                    message=(
                        f"required nested field {sub_fname!r} of "
                        f"{nested_model.__name__} is missing"
                    ),
                )
            continue
        sub_annotation = _annotation_with_constraints(sub_finfo)
        yield from _validate_field_value(
            step_id=step_id,
            tool_spec=tool_spec,
            field_name=f"{field_name}.{sub_fname}",
            value=canonical_value[sub_fname],
            annotation=sub_annotation,
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
        # Codex R4 MAJOR #2: container shell length is statically
        # knowable — refs don't change list length (they substitute
        # in-place, not splice). Check container-level constraints
        # like Field(min_length=2) before recursing into elements.
        yield from _check_container_shell_constraints(
            step_id=step_id,
            tool_spec=tool_spec,
            field_name=field_name,
            value=value,
            annotation=annotation,
        )
        element_annotation = _peel_container_element_annotation(annotation)
        if element_annotation is None:
            return
        for idx, item in enumerate(value):
            yield from _validate_field_value(
                step_id=step_id,
                tool_spec=tool_spec,
                field_name=f"{field_name}[{idx}]",
                value=item,
                annotation=element_annotation,
            )
        return

    if isinstance(value, dict) and contains_ref(value):
        # Codex R4 MAJOR #1: nested BaseModel branch. If annotation is
        # a BaseModel subclass (possibly wrapped in Optional/Annotated),
        # treat the dict as a sub-model and recurse with model_fields
        # context — checks unknown/missing/duplicate per the sub-model's
        # own contract.
        nested_model = _try_extract_basemodel(annotation)
        if nested_model is not None:
            yield from _validate_nested_basemodel_partial(
                step_id=step_id,
                tool_spec=tool_spec,
                field_name=field_name,
                value=value,
                nested_model=nested_model,
            )
            return

        # Container shell constraints (Codex R4 MAJOR #2).
        yield from _check_container_shell_constraints(
            step_id=step_id,
            tool_spec=tool_spec,
            field_name=field_name,
            value=value,
            annotation=annotation,
        )
        # Codex R4 MINOR #4: ref-like dict keys → unsupported_ref_location.
        for k in value.keys():
            if isinstance(k, str) and contains_ref(k):
                yield Violation(
                    step_id=step_id,
                    tool=tool_spec.name,
                    field_path=f"{field_name}.<key {k!r}>",
                    code="unsupported_ref_location",
                    message=(
                        f"reference {k!r} appears as a DICT KEY in field "
                        f"{field_name!r}. References in dict keys are not "
                        f"supported — they aren't resolved by executor."
                    ),
                )
        # Recurse into values + concrete keys for plain dict[K, V].
        value_annotation = _peel_container_element_annotation(annotation)
        key_annotation = _peel_dict_key_annotation(annotation)
        if value_annotation is None and key_annotation is None:
            return
        for k, item in value.items():
            if key_annotation is not None and not (isinstance(k, str) and contains_ref(k)):
                yield from _validate_field_value(
                    step_id=step_id,
                    tool_spec=tool_spec,
                    field_name=f"{field_name}.<key {k!r}>",
                    value=k,
                    annotation=key_annotation,
                )
            if value_annotation is not None:
                yield from _validate_field_value(
                    step_id=step_id,
                    tool_spec=tool_spec,
                    field_name=f"{field_name}[{k!r}]",
                    value=item,
                    annotation=value_annotation,
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


def _allowed_status_literals(output_model: Any) -> set[str]:
    """Codex Sub-A12 Phase B.1 R3 MAJOR: extract every Literal value the
    ``status`` discriminator can take across the discriminated union.

    Used by Phase 3 branch-match validation. Returns empty set if the
    output_model shape is unrecognised (e.g. plain model without a
    ``status`` Literal); callers treat empty set as "skip the check"
    so legacy/edge-case tools don't trip on branch validation."""
    statuses: set[str] = set()
    try:
        from typing import get_args
        union_t = get_args(output_model)[0] if get_args(output_model) else output_model
        for member in get_args(union_t):
            status_field = getattr(member, "model_fields", {}).get("status")
            if status_field is None:
                continue
            for lit_value in get_args(status_field.annotation):
                statuses.add(str(lit_value))
    except Exception:  # noqa: BLE001 — defensive against unusual shapes
        return set()
    return statuses


def _phase3_validate_branch_matches(
    step_id: str,
    action: Action,
    tool_spec: ToolSpec,
) -> Iterator[Violation]:
    """Codex Sub-A12 Phase B.1 R3 MAJOR: validate every
    ``expected_outcomes[].match`` value against the tool's output_model.

    Currently checks just the ``status`` field (the planner's primary
    branching dimension) — ``status`` MUST be one of the Literal values
    declared in the discriminated union. Catches invented statuses like
    ``all_duplicate`` (does not exist in add_shopping_items output)."""
    allowed = _allowed_status_literals(tool_spec.output_model)
    if not allowed:
        # Output model has no status Literal we can introspect — skip check.
        return
    for branch_idx, branch in enumerate(action.expected_outcomes):
        match_dict = branch.match or {}
        status_value = match_dict.get("status")
        if status_value is None:
            continue  # branch matches on other fields; not validated here
        if not isinstance(status_value, str):
            yield Violation(
                step_id=step_id,
                tool=action.tool,
                code="branch_match_status_wrong_type",
                message=(
                    f"expected_outcomes[{branch_idx}].match.status must be "
                    f"a string Literal value; got {type(status_value).__name__}"
                ),
            )
            continue
        if status_value not in allowed:
            yield Violation(
                step_id=step_id,
                tool=action.tool,
                code="branch_match_status_invented",
                message=(
                    f"expected_outcomes[{branch_idx}].match.status="
                    f"{status_value!r} is NOT a real Literal in "
                    f"{action.tool}.output_model. Allowed: "
                    f"{sorted(allowed)}"
                ),
            )


def _phase3_check_catch_all_position(
    step_id: str,
    action: Action,
) -> Iterator[Violation]:
    """#26 — reject a misplaced catch-all branch (empty ``match: {}``).

    A catch-all matches ANY tool output, so the executor's first-match scan
    (``_match_branch``) would never reach branches AFTER it — they are dead
    code. The executor defensively ignores a non-final catch-all at runtime
    (and logs a warning), but per the «catch it at validation, not silently
    at runtime» contract (same rationale as the #36 ref checks) we reject it
    up front so the planner gets a normal invalid-plan retry. A catch-all is
    valid ONLY as the final branch (or as the sole branch).

    Structural check — independent of the tool's output_model, so it runs
    even when status Literals can't be introspected."""
    outcomes = action.expected_outcomes
    last_idx = len(outcomes) - 1
    for idx, branch in enumerate(outcomes):
        if not branch.match and idx != last_idx:
            yield Violation(
                step_id=step_id,
                tool=action.tool,
                code="misplaced_catch_all_branch",
                message=(
                    f"expected_outcomes[{idx}] is a catch-all (empty match) "
                    f"but is not the last of {last_idx + 1} branches — every "
                    f"branch after it is unreachable. Move the catch-all to "
                    f"the final position or give it explicit match fields."
                ),
            )


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
    # Phase 1.b: malformed ${...} tokens (Codex R3 MINOR #5)
    violations.extend(_phase1_check_ref_syntax(step_id, action, tool_spec))
    # Phase 1.b.2: refs in dict KEYS — unsupported (Codex R4 MINOR #4)
    violations.extend(
        _phase1_check_ref_in_dict_keys(step_id, action, tool_spec)
    )
    # Phase 1.c: ref integrity (requires plan context for target lookup)
    if plan is not None:
        violations.extend(
            _phase1_check_refs(step_id, action, plan, tool_spec)
        )
    # Phase 1.d: ToolSpec-declared «at least one of these mutable fields
    # must be provided non-null» — Codex Sub-A4 R5/R6 MAJOR #1. Fires
    # regardless of refs (the rule is structural: are the keys present
    # with non-null values, not «what would they resolve to»).
    violations.extend(
        _phase1_check_required_any_non_null(step_id, action, tool_spec)
    )
    # Phase 2: schema-aware partial
    violations.extend(_phase2_validate_args(step_id, action, tool_spec))
    # Phase 3: branch-match status validation against output_model.
    # Codex Sub-A12 Phase B.1 R3 MAJOR — catches invented statuses
    # (e.g. ``all_duplicate``) at validation time, not at run-time.
    violations.extend(_phase3_validate_branch_matches(step_id, action, tool_spec))
    # Phase 3.b: structural branch-ordering — a catch-all (empty match)
    # must be the last branch, else later branches are unreachable (#26).
    violations.extend(_phase3_check_catch_all_position(step_id, action))
    return violations


def _output_union_members(output_model: Any) -> tuple[type, ...]:
    """Extract the tuple of pydantic BaseModel members from an
    Annotated[Union[A, B, ...], Field(discriminator=...)] type.

    Returns empty tuple if shape is not a discriminated union (e.g.
    a plain BaseModel — caller treats that as "single-member union")."""
    try:
        from typing import get_args
        # Annotated wraps Union — first arg is the Union itself
        anno_args = get_args(output_model)
        if not anno_args:
            # Plain class — treat as single member
            return (output_model,) if isinstance(output_model, type) else ()
        union_t = anno_args[0]
        members = get_args(union_t)
        return tuple(m for m in members if isinstance(m, type))
    except Exception:  # noqa: BLE001
        return ()


def _filter_members_by_status(
    members: tuple[type, ...], status_value: str | None,
) -> tuple[type, ...]:
    """Codex Sub-A12 Phase B.1 R7 MAJOR: narrow union members to those
    whose ``status`` Literal contains ``status_value``. Used for
    branch-compose ref validation where the branch only fires when
    ``status == status_value`` — so refs must resolve against ONLY
    that member, not the whole union.

    If ``status_value`` is None (root compose, no specific status) —
    returns all members."""
    from typing import get_args
    if status_value is None:
        return members
    matched: list[type] = []
    for member in members:
        try:
            sf = member.model_fields.get("status")
            if sf is None:
                continue
            lit_values = get_args(sf.annotation)
            if status_value in (str(v) for v in lit_values):
                matched.append(member)
        except Exception:  # noqa: BLE001
            continue
    return tuple(matched)


def _flatten_union_arms(annotation: Any) -> list[Any] | None:
    """If ``annotation`` (after stripping an outer ``Annotated``) is a
    ``Union`` / ``X | Y``, return its non-None arms with NESTED unions
    flattened and per-arm ``Annotated`` stripped; otherwise None.

    Handles the shapes a flat distinct-model count missed (Codex #36 R2
    MAJOR): ``Optional[A | B]`` (= ``Union[A | B, None]`` — a nested-union
    arg) and ``Annotated[A, ...] | Annotated[B, ...]`` (per-arm Annotated)."""
    inner = annotation
    while get_origin(inner) is Annotated:
        inner = get_args(inner)[0]
    if get_origin(inner) not in (Union, types.UnionType):
        return None
    arms: list[Any] = []
    for arm in get_args(inner):
        if arm is type(None):
            continue
        nested = _flatten_union_arms(arm)
        if nested is not None:
            arms.extend(nested)
            continue
        stripped = arm
        while get_origin(stripped) is Annotated:
            stripped = get_args(stripped)[0]
        arms.append(stripped)
    return arms


def _is_sequence_origin(origin: Any) -> bool:
    """True for concrete ``list``/``set``/``frozenset``/``tuple`` AND
    abstract ``collections.abc.Sequence``/``Set`` parameterizations (e.g.
    ``Sequence[Model]`` — Codex #36 R2 MEDIUM). The runtime resolver never
    projects through any of these. ``str``/``bytes`` are excluded — they
    aren't field-bearing containers."""
    if origin in (list, set, frozenset, tuple):
        return True
    if isinstance(origin, type) and origin not in (str, bytes):
        return issubclass(
            origin, (collections.abc.Sequence, collections.abc.Set)
        )
    return False


def _annotation_path_status(annotation: Any, segments: list[str]) -> bool | None:
    """Can the runtime ref resolver walk the remaining ``segments`` into a
    value of ``annotation``? (#36)

    Mirrors ``interpolation._resolve_path`` EXACTLY: a segment is resolved
    by dict-key lookup OR attribute access — sequences are NEVER projected.

    - ``True``  : resolvable against an introspectable nested BaseModel.
    - ``False`` : guaranteed runtime failure (a segment remains after a
      sequence — e.g. ``${s1.recipe.ingredients.name}``).
    - ``None``  : indeterminate (dynamic dict keys, scalar-with-attrs like
      ``datetime.year``, ``Any``, or an ambiguous union arm) → defer.

    Unions are combined tri-state across ALL flattened, Annotated-stripped
    arms — never collapsed to a single arm (Codex #36 MAJOR #2)."""
    if not segments:
        return True  # ref ends at this value — any type acceptable

    arms = _flatten_union_arms(annotation)
    if arms is not None:
        saw_true = saw_indeterminate = False
        for arm in arms:
            r = _annotation_path_status(arm, segments)
            if r is True:
                saw_true = True
            elif r is None:
                saw_indeterminate = True
        if saw_true:
            return True  # valid against at least one arm
        if saw_indeterminate:
            return None  # can't disprove on some arm → defer
        return False  # every arm definitively rejects

    bare = _strip_optional_and_annotated(annotation)
    nested = _try_extract_basemodel(bare)
    if nested is not None:
        return _member_path_status(nested, segments)
    if _is_sequence_origin(get_origin(bare)):
        # Runtime cannot project a remaining segment through a sequence.
        return False
    # dict (dynamic keys) / scalar-with-attrs / Any / custom — not
    # statically decidable → defer to runtime.
    return None


def _member_path_status(model: Any, segments: list[str]) -> bool | None:
    """Walk ``segments`` against a single BaseModel ``model`` (#36).

    - ``True``  — first segment is a field and the deeper path (if any)
      resolves.
    - ``False`` — a segment is definitively absent on this introspectable
      model: a real bug worth flagging.
    - ``None``  — indeterminate (model not introspectable / deeper shape
      opaque or ambiguous) → defer to runtime.

    Recursion is bounded by ``len(segments)`` (each level consumes one
    segment), so self-referential model graphs cannot loop forever."""
    if not segments:
        return True
    first = segments[0]
    if first.startswith("_"):
        return True  # dunder/private — runtime owns this (separate guard)
    try:
        fields = model.model_fields
    except Exception:  # noqa: BLE001
        return None  # not introspectable — skip
    if first not in fields:
        return False
    if len(segments) == 1:
        return True
    return _annotation_path_status(fields[first].annotation, segments[1:])


def _ref_field_exists_in_output(
    output_model: Any,
    segments: list[str],
    *,
    narrow_to_status: str | None = None,
) -> bool | None:
    """Walk ``segments`` against ``output_model``. Returns True if the
    path is valid in AT LEAST ONE relevant union member; False if every
    relevant member definitively rejects it; None if introspection cannot
    decide (treated as "skip the check" so legacy/edge-case models don't
    trip).

    If ``narrow_to_status`` is set (branch compose case), only union
    members whose status Literal == narrow_to_status are considered —
    this catches the R6→R7 bug where branch compose for status='error'
    refs a field that only exists on the success variant.

    #36: nested segments ARE walked — for ``${s1.recipe.bogus}`` where
    ``recipe`` is a nested ``BaseModel`` without a ``bogus`` field, this
    returns False. Walking stops (returns None → no violation) the moment
    a field is opaque (dict/Any/primitive), so unseeable shapes never
    false-positive."""
    members = _output_union_members(output_model)
    if not members:
        return None  # opaque shape — caller skips check

    if narrow_to_status is not None:
        members = _filter_members_by_status(members, narrow_to_status)
        if not members:
            return None  # no member matches this status — skip
                         # (status validity is checked elsewhere by phase 3)

    if not segments:
        return True  # no path to walk (shouldn't happen for refs)

    first = segments[0]
    if first.startswith("_"):
        # Dunder/private — separately caught by resolve_refs at runtime;
        # not a structural concern here.
        return True

    # Union semantics: True if ANY member fully validates the path; else
    # None if at least one member is indeterminate (defer, don't
    # false-positive); else False (every member definitively rejects it).
    saw_indeterminate = False
    saw_definitive_reject = False
    for member in members:
        status = _member_path_status(member, segments)
        if status is True:
            return True
        if status is None:
            saw_indeterminate = True
        else:
            saw_definitive_reject = True
    if saw_indeterminate:
        return None
    return False if saw_definitive_reject else None


def _parse_ref_segments(ref_path: str) -> list[str]:
    """Split ``s1.field1.field2`` into ``[field1, field2]`` (drops step
    id which is consumed via extract_step_id)."""
    parts = ref_path.split(".")
    if len(parts) <= 1:
        return []
    return parts[1:]


def _phase1_check_compose_refs(
    plan: Plan, registry: Mapping[str, ToolSpec] | None = None,
) -> Iterator[Violation]:
    """Codex Sub-A12 Phase B.1 R4/R5/R7 MAJOR: validate ``${...}`` refs
    that live in ``compose.template_data`` — both root-level Plan compose
    and per-branch composes inside each action's ``expected_outcomes``.

    R4: catch refs to non-existent steps (``${s99.foo}``).
    R5: catch refs to existing step but non-existent top-level output
        field (``${s1.items}`` where add_shopping_items has only
        ``added_count``/``item_ids``).
    R7: variant-aware narrowing for branch composes — when a branch
        fires on ``{status: "X"}``, host-step refs are validated only
        against the union member with that status.

    Known TODO (Phase B.6 — Codex R7 high MAJOR open):
        Root compose refs are validated union-wide. When a branch with
        ``next=None`` AND ``compose=None`` falls through to root compose,
        a ref like ``${s1.added_count}`` in root.template_data can pass
        (added_count exists on success variants) but blow up at runtime
        if the actual outcome was an error variant. Closing this needs
        fallthrough-statuses analysis — out of scope for B.1. For now:
        planner must use refs in root compose that work across all
        statuses, or use literals.

    Self-ref check applies to branch composes (inside a host action):
    a branch compose cannot reference its own host step's pre-execution
    state but CAN reference its own output (the branch fires post-output).
    Per Sub-A12 B.1: self-ref allowed in branch composes."""
    # Root compose — refs may target any step, no self-ref scope
    for ref_path in iter_refs(plan.compose.template_data):
        target = extract_step_id(ref_path)
        if target not in plan.actions:
            yield Violation(
                step_id=None,
                tool=None,
                code="compose_ref_unknown_target",
                message=(
                    f"root compose ref '${{{ref_path}}}' targets unknown "
                    f"step {target!r}. Available: "
                    f"{sorted(plan.actions.keys())}"
                ),
            )
            continue
        if registry is not None:
            yield from _maybe_check_field_path(
                ref_path=ref_path,
                target_step_id=target,
                plan=plan,
                registry=registry,
                location="root compose",
                host_step_id=None,
                host_tool=None,
            )

    # Branch composes — owned by a host action; self-ref forbidden
    for host_step_id, action in plan.actions.items():
        for branch_idx, branch in enumerate(action.expected_outcomes):
            if branch.compose is None:
                continue
            for ref_path in iter_refs(branch.compose.template_data):
                target = extract_step_id(ref_path)
                location = (
                    f"{host_step_id}.expected_outcomes[{branch_idx}].compose"
                )
                if target not in plan.actions:
                    yield Violation(
                        step_id=host_step_id,
                        tool=action.tool,
                        code="compose_ref_unknown_target",
                        message=(
                            f"branch compose at {location} ref "
                            f"'${{{ref_path}}}' targets unknown step "
                            f"{target!r}. Available: "
                            f"{sorted(plan.actions.keys())}"
                        ),
                    )
                    continue
                if registry is not None:
                    # R7 MAJOR fix — narrow union members to those
                    # matching the branch's status. Branch compose at
                    # {status: "error"} can only reference fields from
                    # the error variant of the HOST step (the one that
                    # fired the branch).
                    #
                    # R7 medium follow-up (post-ceiling fix): narrowing
                    # ONLY applies when ref points back at the host
                    # step itself. Cross-step refs (e.g. branch on s2
                    # with status="ok" ref'ing ${s1.foo}) carry no
                    # status info about s1 — narrowing would silently
                    # skip the check because no s1 member has
                    # status="ok". Use union-wide check for cross-step.
                    branch_status = (branch.match or {}).get("status")
                    narrow = (
                        branch_status
                        if (isinstance(branch_status, str) and target == host_step_id)
                        else None
                    )
                    yield from _maybe_check_field_path(
                        ref_path=ref_path,
                        target_step_id=target,
                        plan=plan,
                        registry=registry,
                        location=f"branch compose at {location}",
                        host_step_id=host_step_id,
                        host_tool=action.tool,
                        narrow_to_status=narrow,
                    )


def _maybe_check_field_path(
    *,
    ref_path: str,
    target_step_id: str,
    plan: Plan,
    registry: Mapping[str, ToolSpec],
    location: str,
    host_step_id: str | None,
    host_tool: str | None,
    narrow_to_status: str | None = None,
) -> Iterator[Violation]:
    """Helper: yield a violation if ref's top-level field doesn't exist
    in target tool's output_model. Skip silently if the output_model
    shape is opaque (e.g. test stubs without proper Union shape).

    If ``narrow_to_status`` is set (branch compose), validate refs
    against ONLY the union member whose status Literal matches —
    catches Codex R7 case where branch for status='error' refs a
    field that only exists on the success variant."""
    target_action = plan.actions[target_step_id]
    target_spec = registry.get(target_action.tool)
    if target_spec is None:
        return  # unknown_tool already reported separately
    segments = _parse_ref_segments(ref_path)
    if not segments:
        return  # bare ${stepN} — nothing to validate
    result = _ref_field_exists_in_output(
        target_spec.output_model, segments, narrow_to_status=narrow_to_status,
    )
    if result is False:
        members = _output_union_members(target_spec.output_model)
        if narrow_to_status is not None:
            members = _filter_members_by_status(members, narrow_to_status)
        # Collect available top-level fields across the relevant union members
        available: set[str] = set()
        for m in members:
            try:
                available.update(m.model_fields.keys())
            except Exception:  # noqa: BLE001
                continue
        status_note = (
            f" (narrowed to status={narrow_to_status!r})"
            if narrow_to_status is not None
            else ""
        )
        yield Violation(
            step_id=host_step_id,
            tool=host_tool,
            code="compose_ref_unknown_field",
            message=(
                f"{location} ref '${{{ref_path}}}': field {segments[0]!r} "
                f"is NOT in {target_action.tool}.output_model{status_note}. "
                f"Available top-level fields: "
                f"{sorted(available) or '(none)'}"
            ),
        )


def _iter_all_composes(plan: Plan) -> Iterator[tuple[str | None, str, Any]]:
    """Yield every ``ComposerCall`` in a plan as
    ``(host_step_id, location_label, compose)``.

    Covers the root ``plan.compose`` (host_step_id=None) and every
    branch compose inside each action's ``expected_outcomes``. Skips
    branches with ``compose is None`` (those fall through to root)."""
    yield (None, "root compose", plan.compose)
    for host_step_id, action in plan.actions.items():
        for branch_idx, branch in enumerate(action.expected_outcomes):
            if branch.compose is None:
                continue
            location = (
                f"{host_step_id}.expected_outcomes[{branch_idx}].compose"
            )
            yield (host_step_id, location, branch.compose)


def _llm_required_key_present(value: Any) -> bool:
    """A required ``template_data`` value counts as present at PLAN time
    if it's a ``${...}`` ref (executor resolves it later) or a non-blank
    literal. Mirrors ``LLMPromptRegistry.validate_data`` blank-logic but
    treats unresolved refs as present (they're non-blank strings, but be
    explicit). 0 / False are valid; None / ""/"  " / []/{} are blank."""
    if isinstance(value, str):
        return bool(value.strip())  # refs are non-blank → present
    return value is not None and value != [] and value != {}


def _check_composer_allowlist(
    plan: Plan,
    *,
    composer_template_ids: frozenset[str] | None,
    composer_llm_prompt_keys: frozenset[str] | None,
    llm_prompt_required_keys: Mapping[str, frozenset[str]] | None = None,
) -> Iterator[Violation]:
    """Sub-A12 Phase D.2-enable — plan-time membership check for every
    ComposerCall's ``template_id`` / ``llm_prompt_key`` against the
    registries the planner was shown.

    Why plan-time (not just compose-time runtime fallback): once the
    planner can emit ``kind='llm'`` (D.2-enable), an unknown/stale
    ``llm_prompt_key`` would otherwise pass validation, the plan's
    actions would EXECUTE (committing side effects), and only THEN
    would compose() fail late into a fallback reply. Catching it at
    validate_plan turns it into a normal invalid-plan retry BEFORE any
    tool runs. Same parity now applied to ``template_id`` (Codex
    D.2 R2 — previously template_id was also only runtime-checked).

    Allowlists are ``None`` → skip the check (back-compat: callers that
    only pass plan+registry, and the many existing tests, keep working
    unchanged). When provided, membership is enforced.

    Note: schema already guarantees kind='template' ⇒ template_id set
    and kind='llm' ⇒ llm_prompt_key set (ComposerCall validator), so
    we only check membership here, not presence.
    """
    for host_step_id, location, compose in _iter_all_composes(plan):
        if compose.kind == "template":
            if (
                composer_template_ids is not None
                and compose.template_id not in composer_template_ids
            ):
                yield Violation(
                    step_id=host_step_id,
                    tool=None,
                    code="unknown_template_id",
                    message=(
                        f"{location}: template_id "
                        f"{compose.template_id!r} not in composer registry. "
                        f"Available: {sorted(composer_template_ids)}"
                    ),
                )
        elif compose.kind == "llm":
            key = compose.llm_prompt_key
            if (
                composer_llm_prompt_keys is not None
                and key not in composer_llm_prompt_keys
            ):
                yield Violation(
                    step_id=host_step_id,
                    tool=None,
                    code="unknown_llm_prompt_key",
                    message=(
                        f"{location}: llm_prompt_key "
                        f"{key!r} not in LLM prompt "
                        f"registry. Available: "
                        f"{sorted(composer_llm_prompt_keys)}"
                    ),
                )
                continue
            # Sub-A12 D.2-enable (Codex R1 medium MAJOR + high HIGH #2)
            # — required-data check at PLAN time. Without it, a plan with
            # a valid llm_prompt_key but missing required template_data
            # passes validation, executes write tools, then fails late in
            # the composer (registry.validate_data) into a fallback reply
            # — recreating the "side effects committed, then late compose
            # failure" class the allowlist exists to remove. Refs count
            # as present (executor resolves them).
            if llm_prompt_required_keys is not None and key is not None:
                required = llm_prompt_required_keys.get(key)
                if required:
                    data = compose.template_data or {}
                    missing = sorted(
                        rk for rk in required
                        if not _llm_required_key_present(data.get(rk))
                    )
                    if missing:
                        yield Violation(
                            step_id=host_step_id,
                            tool=None,
                            code="llm_compose_missing_data",
                            message=(
                                f"{location}: llm_prompt_key {key!r} requires "
                                f"template_data keys {missing} (missing/blank). "
                                f"Present: {sorted(data.keys())}"
                            ),
                        )


def validate_plan(
    plan: Plan,
    registry: Mapping[str, ToolSpec],
    *,
    composer_template_ids: frozenset[str] | None = None,
    composer_llm_prompt_keys: frozenset[str] | None = None,
    llm_prompt_required_keys: Mapping[str, frozenset[str]] | None = None,
) -> list[Violation]:
    """Walk every Action and aggregate violations. Returns structured
    list — empty == plan ready to execute. Callers convert to strings
    via ``render_violations`` for retry feedback.

    Sub-A12 Phase D.2-enable: optional ``composer_template_ids`` /
    ``composer_llm_prompt_keys`` allowlists. When provided, every
    ComposerCall's id is checked for membership (so an unknown
    template_id / llm_prompt_key is caught BEFORE the plan executes,
    not as a late compose-time fallback). When omitted (None) the
    check is skipped — back-compat for plan+registry-only callers/tests.
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
    # Codex Sub-A12 Phase B.1 R4/R5 MAJOR — compose template_data refs
    # are now checked for: (R4) target step existence + (R5) top-level
    # field existence in target tool's output_model.
    violations.extend(_phase1_check_compose_refs(plan, registry=registry))
    # Sub-A12 Phase D.2-enable — composer id allowlist membership.
    violations.extend(_check_composer_allowlist(
        plan,
        composer_template_ids=composer_template_ids,
        composer_llm_prompt_keys=composer_llm_prompt_keys,
        llm_prompt_required_keys=llm_prompt_required_keys,
    ))
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
