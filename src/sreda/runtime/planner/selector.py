"""Executor-level ``.only`` selector resolution (PR-b, Piece 2 static side).

The ``.only`` selector is a plan-ref extension:

    ${s1.items.only.reminder_id}

Means: look at step s1's output field ``items`` (a list); if it contains
EXACTLY ONE element, resolve to that element and continue walking
``.reminder_id``.  If the list has 0 or >1 elements, raise
``SelectorAmbiguityError`` — the executor treats this as a pre-invoke abort
(same path as ``arg_violation``, no tool is invoked, no ledger row opened).

Design constraints (plan §PR-b):
- ``interpolation._resolve_path`` stays PURE — no selector semantics there.
- Resolution is executor-level because the executor owns ``StepResult``
  outputs + ``ToolSpec`` access (needed for public labels).
- interpolation.py stays the single source of truth for plain ref resolution.
- ``.first`` / ``.latest`` are OUT of scope; only ``.only`` is implemented here.

Public API
----------
``SelectorAmbiguityError``   — raised when ``.only`` resolves to 0 or >1 items.
``selector_public_label``    — produce a user-visible, id-stripped label for one
                               list item.
``resolve_selector_refs``    — executor pre-pass: rewrite ``${sX.f.only.g}``
                               refs in the args dict before ``resolve_refs``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sreda.runtime.planner.interpolation import (
    InvalidReferenceError,
    _resolve_path as _interp_resolve_path,  # noqa: PLC2701
)


# ---------------------------------------------------------------------------
# Sentinel — prevents double-resolution by resolve_refs (FIX 1, PR-b)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ResolvedLiteral:
    """Opaque wrapper for a selector-resolved value.

    ``interpolation.resolve_refs`` passes any non-(str/dict/list) leaf
    through unchanged (see ``interpolation.resolve_refs`` → ``return value``
    for scalars).  By wrapping every selector-resolved value in this frozen
    dataclass we guarantee that the subsequent ``resolve_refs`` call in the
    executor cannot re-interpret the value as a planner ref — even when the
    resolved value is a string that itself contains ``${...}`` tokens (e.g.
    a reminder title or a ``raw_line`` value that mentions another step).

    After ``resolve_refs`` finishes, the executor calls
    ``unwrap_resolved(args)`` to peel away all ``_ResolvedLiteral`` wrappers
    and recover the plain Python values.

    Mixed-string case (text + embedded selector ref):
    The combined string produced by resolving the selector ref and splicing it
    into the surrounding text is ALSO wrapped in ``_ResolvedLiteral`` so the
    textual result is not re-scanned by ``resolve_refs``.  Plain
    ``${plain_ref}`` tokens in the *surrounding* text are resolved FIRST (by
    keeping them as-is during the selector pre-pass so ``resolve_refs`` can
    handle them normally), then the selector-ref portion is resolved and
    the whole result is wrapped.  This is the "sentinel-wrap mixed results"
    strategy documented in the PR-b fix spec.
    """

    value: Any


def unwrap_resolved(value: Any) -> Any:
    """Recursively unwrap ``_ResolvedLiteral`` sentinels after ``resolve_refs``.

    Call this in the executor after ``resolve_refs`` to recover the plain
    Python values originally produced by the selector pre-pass.
    """
    if isinstance(value, _ResolvedLiteral):
        # Unwrap one level; the inner value itself must not contain sentinels
        # (selector resolution is never recursive), but call recursively to
        # be defensive.
        return unwrap_resolved(value.value)
    if isinstance(value, dict):
        return {k: unwrap_resolved(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap_resolved(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SelectorAmbiguityError(ValueError):
    """Raised when a ``.only`` selector cannot resolve to exactly one item.

    Attributes
    ----------
    selector : str
        The selector segment name (always ``"only"`` for PR-b).
    ref_path : str
        The full original ref path, e.g. ``"s1.items.only.reminder_id"``.
    count : int
        Actual number of items in the list (0 or >1).
    options_public : list[str]
        Human-readable, id-stripped labels for the options (up to 5).
        Empty when count == 0 or no safe label can be derived.
    """

    def __init__(
        self,
        *,
        selector: str,
        ref_path: str,
        count: int,
        options_public: list[str],
    ) -> None:
        self.selector = selector
        self.ref_path = ref_path
        self.count = count
        self.options_public = options_public
        if count == 0:
            detail = "list is empty (0 items)"
        else:
            detail = f"{count} items found — need exactly 1"
        super().__init__(
            f"Selector '.only' on ref path '${{{ref_path}}}' is ambiguous: "
            f"{detail}."
        )


# ---------------------------------------------------------------------------
# Public-label builder
# ---------------------------------------------------------------------------

# Patterns that look like internal ids — strip them before showing to user.
# Prefixes derived from common.py id-type definitions (ShoppingItemId,
# ReminderId, TaskId, ChecklistId, RecipeId, MenuPlanId, MenuItemId,
# FamilyMemberId, ChecklistItemId) plus legacy/extra prefixes for safety.
# All use the shape <prefix>_<24 lowercase hex chars> at runtime; the regex
# accepts 16+ hex chars to stay robust against minor length variations.
_ID_PATTERN = re.compile(
    r"\b(?:"
    r"rem"          # ReminderId        rem_
    r"|rec"         # RecipeId          rec_
    r"|sh"          # ShoppingItemId    sh_
    r"|task"        # TaskId            task_
    r"|checklist"   # ChecklistId       checklist_
    r"|clitem"      # ChecklistItemId   clitem_
    r"|menu"        # MenuPlanId        menu_
    r"|mpi"         # MenuItemId        mpi_
    r"|fm"          # FamilyMemberId    fm_
    r"|tsk"         # legacy / extra
    r"|chk"         # legacy / extra
    r"|fam"         # legacy / extra
    r"|mem"         # legacy / extra
    r"|ui"          # legacy / extra
    r")_[0-9a-f]{16,}\b",
    re.IGNORECASE,
)


def selector_public_label(item: Any, *, max_len: int = 60) -> str | None:
    """Return a user-visible, id-stripped label for a list item.

    Strategy (in preference order):
    1. If the item has a ``raw_line`` string attribute — strip ids from it
       and use the result (the tool-level raw_line is human-readable).
    2. If the item has a ``title`` or ``name`` string attribute — use it
       (no id stripping needed; these are display-level fields).
    3. If the item is a dict, try ``raw_line``, then ``title``, then ``name``
       keys with the same priority.
    4. Otherwise return ``None`` (caller falls back to count-only label).

    The label is NEVER the ``reminder_id`` / ``recipe_id`` / any ``_id``
    field — those contain internal identifiers that would leak to the user
    and are unsuitable for disambiguation phrasing.

    Returns ``None`` when no safe label can be derived.  Caller should
    fall back to ``"N вариантов — уточни словами"`` (no id, no count leak
    of sensitive data).
    """

    def _clean(raw: str) -> str | None:
        cleaned = _ID_PATTERN.sub("", raw).strip()
        # After stripping ids we may have bare brackets, extra spaces, etc.
        cleaned = re.sub(r"\[\s*\]", "", cleaned).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        if not cleaned:
            return None
        return cleaned[:max_len]

    # Dict-like item
    if isinstance(item, dict):
        for key in ("raw_line", "title", "name"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                # Scrub ids from ALL label sources (FIX 2): title/name can
                # contain embedded ids just as raw_line can.
                result = _clean(val)
                if result:
                    return result
        return None

    # Pydantic / object with attributes
    raw_line = getattr(item, "raw_line", None)
    if isinstance(raw_line, str) and raw_line.strip():
        return _clean(raw_line)

    for attr in ("title", "name"):
        val = getattr(item, attr, None)
        if isinstance(val, str) and val.strip():
            # Scrub ids from title/name too (FIX 2).
            result = _clean(val)
            if result:
                return result

    return None


# ---------------------------------------------------------------------------
# Selector ref pattern
# ---------------------------------------------------------------------------

# Matches the step-id segment and captures it; checks for ".only." anywhere
# in the path.  Full pattern: ${sX.a.b.only.c.d}
#
# We identify selector refs as those whose dotted path contains the literal
# segment "only" (i.e. between two dots, or after the last field-after-step).
# Group 1 = full inner path (without ${}).
_REF_PATTERN = re.compile(r"\$\{([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\}")

_ONLY_SEGMENT = "only"


def _has_only_segment(path: str) -> bool:
    """True if ``path`` has a segment literally equal to 'only'."""
    return _ONLY_SEGMENT in path.split(".")


# ---------------------------------------------------------------------------
# Resolution logic
# ---------------------------------------------------------------------------


def _resolve_only_ref(
    path: str,
    state: dict[str, Any],
) -> Any:
    """Resolve a single ref path containing a ``.only`` selector.

    ``path`` is the inner content of ``${...}``, e.g.
    ``"s1.items.only.reminder_id"``.

    Steps:
    1. Walk segments up to (but not including) ``only`` — this is the
       list field lookup via plain dict traversal.
    2. Assert the result is a ``list``.
    3. If len == 1 → resolve the REMAINING segments after ``only`` against
       the single element using ``interpolation._resolve_path`` semantics
       (dict/attr traversal).
    4. If len != 1 → raise ``SelectorAmbiguityError``.

    Raises
    ------
    InvalidReferenceError
        When a segment before ``only`` can't be walked (unknown node/field).
    SelectorAmbiguityError
        When the list has 0 or >1 elements.
    """
    parts = path.split(".")
    try:
        only_idx = parts.index(_ONLY_SEGMENT)
    except ValueError:
        # Should not happen — caller checks _has_only_segment first.
        raise InvalidReferenceError(
            f"Internal error: ref '${{{path}}}' has no '.only' segment"
        )

    # Parts before only: [step_id, field1, field2, ...]
    before_only = parts[:only_idx]  # e.g. ["s1", "items"]
    after_only = parts[only_idx + 1:]  # e.g. ["reminder_id"] or []

    if len(before_only) < 2:
        raise InvalidReferenceError(
            f"Ref '${{{path}}}': '.only' must be preceded by at least "
            f"'step_id.field' (got {before_only!r})."
        )

    step_id = before_only[0]
    if step_id not in state:
        raise InvalidReferenceError(
            f"Ref '${{{path}}}' refers to unknown step '{step_id}'. "
            f"Available: {sorted(state.keys()) or '(none)'}"
        )

    # Walk segments before 'only' starting from state[step_id].
    current: Any = state[step_id]
    for seg in before_only[1:]:
        if seg.startswith("_"):
            raise InvalidReferenceError(
                f"Ref '${{{path}}}': field '{seg}' starts with underscore "
                f"— private attribute access forbidden."
            )
        if isinstance(current, dict):
            if seg not in current:
                raise InvalidReferenceError(
                    f"Ref '${{{path}}}': field '{seg}' not in "
                    f"result of '{step_id}'. Available: "
                    f"{sorted(current.keys()) or '(none)'}"
                )
            current = current[seg]
        elif hasattr(current, seg):
            current = getattr(current, seg)
        else:
            raise InvalidReferenceError(
                f"Ref '${{{path}}}': cannot access '{seg}' on "
                f"{type(current).__name__}."
            )

    # current must be a list
    if not isinstance(current, list):
        raise InvalidReferenceError(
            f"Ref '${{{path}}}': '.only' selector expects a list, "
            f"but field resolved to {type(current).__name__}."
        )

    count = len(current)
    if count != 1:
        # Build public labels (max 5, id-stripped)
        options_public: list[str] = []
        for item in current[:5]:
            label = selector_public_label(item)
            if label:
                options_public.append(label)

        raise SelectorAmbiguityError(
            selector=_ONLY_SEGMENT,
            ref_path=path,
            count=count,
            options_public=options_public,
        )

    # Exactly one element — resolve remaining segments against it.
    element = current[0]
    if not after_only:
        return element

    # Walk after_only against the element.
    cur: Any = element
    for seg in after_only:
        if seg.startswith("_"):
            raise InvalidReferenceError(
                f"Ref '${{{path}}}': field '{seg}' starts with underscore "
                f"— private attribute access forbidden."
            )
        if isinstance(cur, dict):
            if seg not in cur:
                raise InvalidReferenceError(
                    f"Ref '${{{path}}}': field '{seg}' not in element. "
                    f"Available: {sorted(cur.keys()) or '(none)'}"
                )
            cur = cur[seg]
        elif hasattr(cur, seg):
            cur = getattr(cur, seg)
        else:
            raise InvalidReferenceError(
                f"Ref '${{{path}}}': cannot access '{seg}' on "
                f"{type(cur).__name__}."
            )
    return cur


# ---------------------------------------------------------------------------
# Pre-pass entry point
# ---------------------------------------------------------------------------


def resolve_selector_refs(
    value: Any,
    state: dict[str, Any],
) -> Any:
    """Executor pre-pass: rewrite all ``.only`` refs in ``value`` to their
    resolved values (or raise ``SelectorAmbiguityError`` / ``InvalidReferenceError``).

    After this call, the result contains NO ``.only`` refs — ``resolve_refs``
    from ``interpolation.py`` can then handle any remaining plain refs.

    Only touches strings/dicts/lists recursively.  Scalars pass through.

    The call order in the executor must be:
    1. ``resolve_selector_refs(action.args, snapshot)``  ← this function
    2. ``resolve_refs(result, snapshot)``                ← interpolation

    Raises
    ------
    SelectorAmbiguityError
        When a ``.only`` selector finds 0 or >1 items.
    InvalidReferenceError
        When the path before/after ``.only`` can't be walked.
    """
    if isinstance(value, dict):
        return {k: resolve_selector_refs(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_selector_refs(item, state) for item in value]
    if isinstance(value, str):
        return _resolve_selector_in_string(value, state)
    return value


def _resolve_selector_in_string(text: str, state: dict[str, Any]) -> Any:
    """Handle selector refs within a single string.

    If the entire string is a single ``.only`` ref → return the resolved
    value with type preserved.  If mixed text + ``.only`` ref → resolve the
    ref to str and interpolate.  If the string contains no ``.only`` ref
    at all → return unchanged (``resolve_refs`` will handle plain refs).
    """
    matches = list(_REF_PATTERN.finditer(text))
    if not matches:
        return text

    # Filter to only the matches that contain .only
    only_matches = [m for m in matches if _has_only_segment(m.group(1))]
    if not only_matches:
        return text  # no selector refs; leave for resolve_refs

    # Single full-string selector ref — preserve resolved type, wrap in sentinel.
    if len(matches) == 1 and only_matches and matches[0].group(0) == text:
        return _ResolvedLiteral(_resolve_only_ref(matches[0].group(1), state))

    # Mixed string — resolve BOTH selector refs and plain refs in a single
    # substitution pass, then wrap the result in _ResolvedLiteral.  This is
    # the "combined traversal" strategy: because the result is wrapped, a
    # subsequent resolve_refs call cannot re-scan it for ${...} tokens.
    # Resolving plain refs here (via _interp_resolve_path) is safe: the state
    # snapshot is identical to what resolve_refs would use, and doing it in
    # one pass prevents any ${...} literal inside a selector-resolved value
    # from being mistaken for a planner ref by resolve_refs.
    def replace(m: re.Match) -> str:
        path = m.group(1)
        if _has_only_segment(path):
            resolved = _resolve_only_ref(path, state)
            return str(resolved)
        # Plain ref — resolve inline so the final string is complete and
        # does not contain any ${...} tokens that resolve_refs would re-scan.
        return str(_interp_resolve_path(path, state))

    return _ResolvedLiteral(_REF_PATTERN.sub(replace, text))


# ---------------------------------------------------------------------------
# PR-c 2d — consumer routing helper
# ---------------------------------------------------------------------------
#
# This helper is intentionally in selector.py (not executor.py) so that:
# 1. It can be imported by consumers (replay harness, future PR-2b node)
#    without pulling in the full executor module.
# 2. It stays co-located with the selector logic it describes.
# 3. ExecutionLog is imported lazily (TYPE_CHECKING guard) to avoid a
#    circular import: executor → selector is fine; selector → executor
#    would create a cycle (executor imports selector at the top level).
#
# The TYPE_CHECKING guard means the annotation uses a string forward ref
# at runtime and is resolved only by type checkers.
if TYPE_CHECKING:
    from sreda.runtime.planner.executor import ExecutionLog
    from sreda.runtime.planner.schemas import ComposerCall
    from sreda.services.composer.compose import ComposeResult


# #153: GENERIC honesty-литерал для aborted_partial-уточнения — НИКОГДА из
# сырых имён инструментов (PR-c R1 leak-класс). Единый источник истины:
# и продюсер (clarification_compose_for_abort ниже), и order-preserving guard
# в planner_chat._selector_clarification_reply импортируют ЭТУ константу, чтобы
# смена текста не могла молча уронить guard (code-review R1: дублированный литерал).
SELECTOR_PARTIAL_DONE_SUMMARY = "часть действий уже выполнила"


def clarification_compose_for_abort(
    execution_log: "ExecutionLog",
) -> "ComposerCall | None":
    """Given an ExecutionLog with ``abort_kind=="selector_ambiguity"``,
    return the appropriate ComposerCall for the consumer to use instead
    of calling ``compose()`` with the plan-level compose.

    Routing (BOTH cases use the runtime-only ``selector_ambiguity_clarification``
    template, which renders the sanitized option labels + count):
    - ``outcome == "aborted"`` (no prior writes) → ``partial=False``: just the
      option labels + count.
    - ``outcome == "aborted_partial"`` (prior writes committed) → ``partial=True``
      plus a GENERIC literal ``done_summary`` acknowledging that some actions were
      performed (NEVER raw tool names — the PR-c R1 leak class), THEN the same
      option labels + count so the partial user still sees what to disambiguate
      (Codex PR-c R4 high).

    Returns ``None`` when there is no ``abort_kind`` to route (caller
    falls through to normal ``compose()`` as usual).

    Most consumers should NOT call this directly — use
    ``compose_for_execution(plan.compose, execution_log, ...)`` (below), which
    wraps this routing decision + ``compose()`` into a single seam so the
    abort→clarification swap can never be skipped. This function stays public
    for callers that need the routing decision on its own.

    Routing decision (what ``compose_for_execution`` applies)::

        abort_call = clarification_compose_for_abort(execution_log)
        effective_call = abort_call if abort_call is not None else plan.compose
        result = compose(effective_call, execution_log, ...)
    """
    # Import here to avoid circular dependency (selector is imported by executor).
    from sreda.runtime.planner.schemas import ComposerCall  # noqa: PLC0415

    if execution_log.abort_kind != "selector_ambiguity":
        return None

    public = execution_log.abort_details_public or {}
    options: list[str] = public.get("options", [])
    count: int = public.get("count", 0)

    if execution_log.outcome == "aborted_partial":
        # Some earlier steps committed writes — acknowledge them, THEN show the
        # ambiguous options so the partial user still knows WHAT to disambiguate.
        # Codex PR-c R4 (high) MAJOR: the prior routing to
        # ``partial_with_clarification`` had no options slot, so the partial user
        # was told "did part of it" but never shown the choices — asymmetric with
        # the plain-aborted path. Use the selector template with ``partial=True``
        # (it carries both the acknowledgement and the option list).
        # done_summary MUST be a GENERIC literal — NEVER built from raw tool
        # names (the PR-c R1 leak class stays closed).
        return ComposerCall.model_validate({
            "kind": "template",
            "template_id": "selector_ambiguity_clarification",
            "template_data": {
                "options": options,
                "count": count,
                "partial": True,
                "done_summary": SELECTOR_PARTIAL_DONE_SUMMARY,
            },
        })

    # Plain aborted — no prior writes.
    return ComposerCall.model_validate({
        "kind": "template",
        "template_id": "selector_ambiguity_clarification",
        "template_data": {
            "options": options,
            "count": count,
            "partial": False,
        },
    })


def compose_for_execution(
    plan_compose_call: "ComposerCall",
    execution_log: "ExecutionLog",
    **compose_kwargs: Any,
) -> "ComposeResult":
    """THE single seam every execute->compose consumer must use.

    Routes a selector-ambiguity abort to its runtime clarification compose;
    otherwise falls through to the planner's plan-level ``ComposerCall``. All
    remaining keyword args (``registry``, ``llm_composer``, ``ctx``,
    snapshot-hash checks, ...) are forwarded verbatim to ``compose()``.

    Why this exists (Codex PR-c R3 medium MAJOR): ``compose()`` deliberately
    "trusts what it's given" and IGNORES branch overrides for non-``completed``
    outcomes (see ``compose._pick_effective_call`` and the ``compose`` docstring
    note on aborted plans). So a consumer that called
    ``compose(plan.compose, execution_log)`` directly on a selector abort would
    silently render the ORIGINAL plan compose instead of the ambiguity
    clarification. The abort->clarification swap is therefore a consumer
    responsibility; centralising it in ONE tracked, tested function means every
    consumer — the replay harness today, the live Phase-E wiring (PR-2b)
    tomorrow — routes aborts correctly and cannot forget the swap (g-035:
    thread new pipeline status through ALL consumers).
    """
    # Deferred import: this is the planner -> composer dependency edge. Kept
    # inside the function (not module-level) to avoid the import cycle
    # validator -> composer/__init__ -> compose -> executor -> ... -> selector.
    from sreda.services.composer import compose  # noqa: PLC0415

    abort_call = clarification_compose_for_abort(execution_log)
    effective_call = abort_call if abort_call is not None else plan_compose_call
    return compose(effective_call, execution_log, **compose_kwargs)


__all__ = [
    "SelectorAmbiguityError",
    "clarification_compose_for_abort",
    "compose_for_execution",
    "selector_public_label",
    "resolve_selector_refs",
    "unwrap_resolved",
]
