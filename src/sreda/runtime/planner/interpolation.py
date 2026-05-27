"""Variable interpolation for plan args / compose data (Sub-A1, Epic #74).

The planner emits args and ``template_data`` values like
``"${s1.recipe.title}"`` to reference the output of an earlier action.
At executor visit-time the engine walks the structure and resolves these
against an in-memory ``state`` map of ``{action_id: output_dict}``.

Semantics:

- A string that is a *single* full reference (e.g. ``"${s1.items}"``)
  resolves to the raw value with its original type preserved
  (list, dict, int, str, bool, None).
- A string with text *plus* references (``"Сделано: ${s1.count}"``) is
  always interpolated to a string — refs are stringified via ``str()``.
- Dicts and lists are walked recursively.
- Scalars (int, float, bool, None) pass through unchanged.

Failures are loud: an unknown node id, missing field, or attribute
access on a non-dict raises ``InvalidReferenceError``. Executor catches
this and aborts the plan with a clean error rather than letting ``None``
silently flow into a tool.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

_REF_PATTERN = re.compile(r"\$\{([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\}")
"""Pattern for ``${node.field.subfield}`` references.

Path consists of non-empty dotted segments. Each segment starts with
a letter or underscore (no ``${123...}``) and contains word chars.
Trailing dots (``${s1.}``) and consecutive dots (``${s1..x}``) are
REJECTED — Codex R2 MINOR #6 spotted these as fall-through cases that
passed Phase 1 and failed only inside ``_resolve_path`` at executor
time.
"""


class InvalidReferenceError(ValueError):
    """Reference cannot be resolved against the supplied state."""


# ---------------------------------------------------------------------------
# Public ref-introspection helpers (Sub-A-77 item #4 R1 MINOR #10)
# ---------------------------------------------------------------------------


def contains_ref(value: Any) -> bool:
    """Return True if ``value`` (or any nested str inside it) contains
    at least one ``${...}`` reference. Recursively walks dicts / lists
    / tuples; non-string leaves can never contain refs.

    Exposed as a public helper so consumers (validator, executor,
    test infrastructure) share the same reference-detection semantics
    as ``resolve_refs``. Underscored ``_REF_PATTERN`` stays private.
    """

    if isinstance(value, str):
        return _REF_PATTERN.search(value) is not None
    if isinstance(value, dict):
        return any(contains_ref(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_ref(item) for item in value)
    return False


def is_full_ref_string(value: Any) -> bool:
    """Return True if ``value`` is a string that is EXACTLY one
    reference with nothing else — i.e. ``"${s1.field}"`` and nothing
    around it. Such strings resolve to the referenced value with type
    preserved (list, dict, int, etc.) per ``resolve_refs`` semantics.

    Mixed strings like ``"prefix ${s1.x}"`` are interpolated as ``str``
    at executor time; they return False here so callers can validate
    them against a string-typed field instead of deferring.
    """

    if not isinstance(value, str):
        return False
    match = _REF_PATTERN.fullmatch(value)
    return match is not None


def iter_refs(value: Any) -> Iterator[str]:
    """Yield every reference path (``"step_id.field..."``) found inside
    ``value`` — recursively walking dicts / lists / tuples. The yielded
    string is the dotted path, NOT the ``${...}`` wrapper.

    Useful for ref-integrity checks (Sub-A-77 item #4 R1 MAJOR #5):
    extract refs, verify target step exists, check dependency
    relationship.
    """

    if isinstance(value, str):
        for match in _REF_PATTERN.finditer(value):
            yield match.group(1)
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_refs(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_refs(item)


def extract_step_id(ref_path: str) -> str:
    """Return the leading step-id segment of a ref path.

    ``"s1.recipe.title"`` → ``"s1"``. Empty path raises ``ValueError``;
    callers should pass paths obtained from ``iter_refs`` which can
    never be empty by regex construction.
    """

    if not ref_path:
        raise ValueError("ref_path is empty")
    return ref_path.split(".", 1)[0]


def resolve_refs(value: Any, state: dict[str, Any]) -> Any:
    """Recursively resolve ``${node.field}`` references inside ``value``.

    See module docstring for type-preservation semantics.

    Args:
        value: Arbitrary value — str, dict, list, scalar.
        state: ``{action_id: action_output}``. Output is typically a dict
            (the validated ``ToolOutput`` model dumped via
            ``.model_dump()``), but the engine also supports pydantic
            BaseModel instances via attribute access.

    Returns:
        Value with references resolved.

    Raises:
        InvalidReferenceError: when a reference points to an unknown node
            or missing field.
    """
    if isinstance(value, dict):
        return {k: resolve_refs(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, state) for item in value]
    if isinstance(value, str):
        return _resolve_in_string(value, state)
    # int / float / bool / None / other scalar — return as-is
    return value


def _resolve_in_string(text: str, state: dict[str, Any]) -> Any:
    """Resolve refs inside a single string value.

    Returns the resolved value with type preserved if ``text`` is exactly
    one reference; otherwise returns an interpolated string.
    """
    matches = list(_REF_PATTERN.finditer(text))
    if not matches:
        return text

    # Single full-string reference — preserve resolved value's type
    if len(matches) == 1 and matches[0].group(0) == text:
        return _resolve_path(matches[0].group(1), state)

    # Mixed text + ref(s) — stringify resolved values for interpolation
    def replace(match: re.Match[str]) -> str:
        return str(_resolve_path(match.group(1), state))

    return _REF_PATTERN.sub(replace, text)


def _resolve_path(path: str, state: dict[str, Any]) -> Any:
    """Resolve a dotted path ``node_id.field.subfield`` against state."""
    parts = path.split(".")
    if not parts or not parts[0]:
        raise InvalidReferenceError(f"Empty reference path '${{{path}}}'")

    node_id, *fields = parts
    if node_id not in state:
        raise InvalidReferenceError(
            f"Reference '${{{path}}}' refers to unknown node '{node_id}'. "
            f"Available nodes: {sorted(state.keys()) or '(none)'}"
        )

    current: Any = state[node_id]
    for field in fields:
        # Dunder / private guard — protects against LLM-hallucinated paths
        # like ${s1.__class__.__name__} or ${s1.__dict__} that would
        # leak internal Python structure into compose output. Code-review
        # 2026-05-25 MAJOR #1.
        if field.startswith("_"):
            raise InvalidReferenceError(
                f"Reference '${{{path}}}' — field '{field}' starts with "
                f"underscore. Private/dunder attribute access is forbidden "
                f"(prevents leakage of internal Python structure)."
            )
        if isinstance(current, dict):
            if field not in current:
                available = sorted(current.keys())
                raise InvalidReferenceError(
                    f"Reference '${{{path}}}' — field '{field}' not in "
                    f"result of '{node_id}'. Available fields: "
                    f"{available or '(none)'}"
                )
            current = current[field]
        elif hasattr(current, field):
            # Pydantic BaseModel / dataclass / object with attribute access
            current = getattr(current, field)
        else:
            raise InvalidReferenceError(
                f"Reference '${{{path}}}' — cannot access field '{field}' on "
                f"value of type {type(current).__name__} (value: {current!r})."
            )
    return current


__all__ = [
    "InvalidReferenceError",
    "contains_ref",
    "extract_step_id",
    "is_full_ref_string",
    "iter_refs",
    "resolve_refs",
]
