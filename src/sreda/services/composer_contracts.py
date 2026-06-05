"""Shared key-allowlist constants and validation logic for ``humanize_result``
LLM prompt data.

Kept in a standalone module that avoids importing the ``composer`` package so
that both ``sreda.services.composer.prompts_registry`` (services layer) and
``sreda.runtime.planner.validator`` (planner layer) can import from here
without creating a circular dependency. Its ONLY intra-project import is the
``sreda.services.clarification_contract`` leaf (itself zero-intra-project-import),
referenced by the per-template contract registry below — see the import note.

The cycle that motivated extraction:
  validator → composer/__init__ → compose → executor → plan_compiler → validator

Placing the constants here (``sreda.services.composer_contracts``) avoids
triggering ``sreda.services.composer.__init__`` on import, because the
module is a sibling of the ``composer`` package directory, not inside it.
"""

from __future__ import annotations

import re
from typing import Protocol

# The ONLY intra-project import allowed here (plan §"NEW leaf module" R3
# MINOR): ``clarification_contract`` is a true zero-intra-project-import leaf,
# so importing it cannot reintroduce the
#   validator → composer/__init__ → compose → executor → plan_compiler → validator
# cycle that motivated extracting this module as a sibling of the composer
# package. Used by the per-template contract registry below so the
# clarification family is referenced — not duplicated.
from sreda.services.clarification_contract import (
    RUNTIME_ONLY_TEMPLATE_IDS,
    validate_clarification_payload,
)

_HUMANIZE_RESULT_ALLOWED_TOP_KEYS: frozenset[str] = frozenset({"intent", "actions"})
"""Strict top-level key allowlist for ``humanize_result`` template_data.

Only ``intent`` and ``actions`` may be present. Any extra key (execution
ids, raw errors, internal fields, user PII) is rejected so it cannot
reach the LLM and be narrated to the user (rot-enablement Phase 1 R1 FIX 3).
"""

_HUMANIZE_RESULT_ACTION_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"user_visible_summary", "status"}
)
"""Strict key allowlist for each item in ``humanize_result.actions``.

Each action item must be exactly ``{user_visible_summary: str, status: str}``.
Raw internal fields (``tool``, ``execution_id``, ``error``, result objects)
are rejected — callers must curate public summaries before passing data here.
"""

# Full-ref pattern: a string that is exactly "${...}" with no surrounding text.
_FULL_REF_RE = re.compile(r"^\$\{[^}]+\}$")


def _is_full_ref(v: object) -> bool:
    """Return True if ``v`` is a string that is exactly one ``${...}`` ref.

    Used at plan-validation time (allow_refs=True) to defer checks on
    values that will only be resolved post-execution.
    """
    return isinstance(v, str) and _FULL_REF_RE.match(v) is not None


def validate_humanize_result_payload(
    data: dict,  # type: ignore[type-arg]
    *,
    allow_refs: bool,
) -> list[str]:
    """Validate the ``humanize_result`` template_data payload.

    Single source of truth used by BOTH:
    - ``prompts_registry._validate_humanize_result_data`` (runtime,
      ``allow_refs=False`` — all values are fully resolved).
    - ``validator._check_composer_allowlist`` (Phase B, ``allow_refs=True``
      — values may still be unresolved ``${...}`` ref strings).

    Returns a list of human-readable error strings; an EMPTY list means
    the payload is valid.

    Rules
    -----
    Top-level keys
        Must be a subset of ``{intent, actions}``. Extra keys are always
        rejected regardless of allow_refs.

    ``intent``
        Must be present.  When ``allow_refs=True`` and the value is a
        full ref string (``"${...}"``), it is accepted as-is (will resolve
        post-execution).  Otherwise it must be a non-empty string.

    ``actions``
        - If ``actions`` is a full ref string AND ``allow_refs=True`` →
          accepted; per-item checks are skipped (value resolves later).
        - Otherwise must be a non-empty list.  Non-list values (strings that
          are not full-refs, ints, dicts, empty lists, …) are rejected.

    Each ``actions`` item
        - If the item is a full ref string AND ``allow_refs=True`` → skip
          (resolves later).
        - Otherwise must be a ``dict`` whose keys are EXACTLY
          ``{user_visible_summary, status}``.  Missing keys, extra keys, and
          non-dict items are rejected.
        - ``user_visible_summary`` and ``status`` must be non-empty strings,
          UNLESS the value is a full ref string AND ``allow_refs=True``.
    """
    errors: list[str] = []

    # ---- top-level key allowlist -----------------------------------------
    extra_top = sorted(set(data.keys()) - _HUMANIZE_RESULT_ALLOWED_TOP_KEYS)
    if extra_top:
        errors.append(
            f"humanize_result template_data has disallowed top-level keys: "
            f"{extra_top}. Only {sorted(_HUMANIZE_RESULT_ALLOWED_TOP_KEYS)} "
            f"are permitted — extra keys may carry internal/PII fields that "
            f"must not reach the LLM."
        )

    # ---- intent ---------------------------------------------------------------
    intent = data.get("intent")
    if intent is None:
        errors.append("humanize_result template_data missing required key 'intent'.")
    elif allow_refs and _is_full_ref(intent):
        pass  # deferred — will resolve post-execution
    elif not isinstance(intent, str) or not intent.strip():
        errors.append(
            f"humanize_result template_data['intent'] must be a non-empty string, "
            f"got {intent!r}."
        )

    # ---- actions --------------------------------------------------------------
    actions = data.get("actions")

    # Full-ref at the top level: defer entire per-item walk
    if allow_refs and _is_full_ref(actions):
        return errors  # top-level check already done above

    if not isinstance(actions, list) or len(actions) == 0:
        errors.append(
            "humanize_result template_data['actions'] must be a non-empty list."
        )
        return errors

    for i, item in enumerate(actions):
        # Item is a full ref → deferred
        if allow_refs and _is_full_ref(item):
            continue

        if not isinstance(item, dict):
            errors.append(
                f"humanize_result template_data['actions'][{i}] must be a dict, "
                f"got {type(item).__name__!r}."
            )
            continue

        # Key exactness check
        extra_item = sorted(set(item.keys()) - _HUMANIZE_RESULT_ACTION_ALLOWED_KEYS)
        if extra_item:
            errors.append(
                f"humanize_result template_data['actions'][{i}] has disallowed "
                f"keys: {extra_item}. Each action item must contain only "
                f"{sorted(_HUMANIZE_RESULT_ACTION_ALLOWED_KEYS)}. "
                f"Raw fields like 'tool', 'execution_id', 'error' must not be "
                f"passed — use 'user_visible_summary' instead."
            )

        # Required value checks for each mandatory item key
        for required_item_key in ("user_visible_summary", "status"):
            val = item.get(required_item_key)
            if val is None:
                errors.append(
                    f"humanize_result template_data['actions'][{i}]"
                    f"[{required_item_key!r}] is missing."
                )
            elif allow_refs and _is_full_ref(val):
                pass  # deferred
            elif not isinstance(val, str) or not val.strip():
                errors.append(
                    f"humanize_result template_data['actions'][{i}]"
                    f"[{required_item_key!r}] must be a non-empty string, "
                    f"got {val!r}."
                )

    return errors


# ---------------------------------------------------------------------------
# Per-template contract registry (issue #88 PR-d, Piece 3)
# ---------------------------------------------------------------------------
# A central, machine-checkable map from ``template_id`` → its payload-validator
# (a ``ComposerContract``) OR the explicit ``NO_CONTRACT`` marker for templates
# that legitimately need none. This makes "every exposed template_id is either
# contracted or explicitly no-contract" testable (the coverage test in
# test_composer_registry.py iterates the live composer REGISTRY and asserts no
# template falls through unclassified).
#
# MOST templates need NO contract: their template_data is either empty, a
# single literal string the renderer prints verbatim, or a shape with no
# leak/crash surface that the schema + StrictUndefined already guard. We do NOT
# invent contracts for those — over-constraining would reject valid planner
# outputs. Within THIS template-id registry the only real contract is:
#   - the clarification family (ask_user_for_clarification /
#     partial_with_clarification) → validate_clarification_payload, referenced
#     from the clarification_contract leaf (NOT duplicated here).
#
# ``humanize_result`` ALSO has a real contract (validate_humanize_result_payload,
# defined in this module) but it is NOT a registry entry: it is an LLM-prompt
# KEY, not a template_id, so the validator dispatches it from the kind='llm'
# branch (unchanged) rather than via get_composer_contract. The validator is
# kept in this module so both paths share one source of truth. The registry's
# template-id keys below are the deterministic Jinja templates only.


class ComposerContract(Protocol):
    """A contract validator: ``(data, *, allow_refs) -> list[str]`` of human-
    readable error strings (empty == valid). Both
    ``validate_humanize_result_payload`` and ``validate_clarification_payload``
    satisfy this shape."""

    def __call__(
        self,
        data: dict,  # type: ignore[type-arg]
        *,
        allow_refs: bool,
    ) -> list[str]: ...


# Sentinel meaning "this template_id was deliberately classified as needing no
# payload contract" — distinct from ``None`` returned for an UNKNOWN id (which
# the coverage test treats as an unclassified failure).
NO_CONTRACT: object = object()


# Authoritative classification of every planner-exposed ``template_id`` in
# ``HOUSEWIFE_TEMPLATES`` (services/composer/templates_housewife.py). Keep in
# sync: the coverage test fails if the composer registry grows a template that
# is neither here nor in RUNTIME_ONLY_TEMPLATE_IDS.
_COMPOSER_CONTRACTS: dict[str, ComposerContract | object] = {
    # ---- real contracts ----------------------------------------------------
    # Clarification family — referenced from the leaf, not duplicated.
    "ask_user_for_clarification": validate_clarification_payload,
    "partial_with_clarification": validate_clarification_payload,
    # ---- explicit "no contract" --------------------------------------------
    # shopping: literal item lists / counts; no leak or crash surface beyond
    # what schema + StrictUndefined already cover.
    "shopping_added_ok": NO_CONTRACT,
    "shopping_added_empty": NO_CONTRACT,
    "shopping_list_show": NO_CONTRACT,
    "shopping_list_empty": NO_CONTRACT,
    # reminders: literal phrases / counts.
    "reminder_set_ok": NO_CONTRACT,
    "reminder_skipped_past": NO_CONTRACT,
    "reminders_list_show": NO_CONTRACT,
    "reminders_list_empty": NO_CONTRACT,
    # checklists: title + literal item lines (title / item_status from the
    # show_checklist output_model); no leak/crash surface beyond schema +
    # StrictUndefined.
    "checklist_show": NO_CONTRACT,
    "checklist_empty": NO_CONTRACT,
    # recipes: passthrough text / query echo.
    "recipe_show": NO_CONTRACT,
    "recipe_not_found_ask_alt": NO_CONTRACT,
    # clarification — reminder-time ask. Only renders ``what`` (a literal the
    # schema already requires non-blank); no closed-enum payload.
    "ask_when_to_remind": NO_CONTRACT,
    # error / fallback — fixed canned text, error_code never rendered.
    "generic_tool_error": NO_CONTRACT,
    "partial_with_compose_error": NO_CONTRACT,
    "invalid_plan_fallback": NO_CONTRACT,
    # conversational / identity — fixed canned text.
    "identity_playful": NO_CONTRACT,
    "smalltalk_fallback": NO_CONTRACT,
}


def get_composer_contract(template_id: str) -> ComposerContract | object | None:
    """Return the per-template payload contract for ``template_id``.

    Returns one of:
    - a ``ComposerContract`` callable — the template carries a structured
      payload that must be validated (call it with ``allow_refs=True`` at
      plan-time, ``allow_refs=False`` at resolved-render time).
    - ``NO_CONTRACT`` (sentinel) — the template was deliberately classified as
      needing no payload contract.
    - ``None`` — the ``template_id`` is UNKNOWN to the registry (NOT the same
      as NO_CONTRACT). Callers/coverage tests treat this as "unclassified".

    RUNTIME-ONLY templates (``RUNTIME_ONLY_TEMPLATE_IDS``) are NOT in this
    registry — they are never planner-emitted, and the validator rejects them
    upstream (``runtime_only_template_in_plan``). The coverage test excludes
    them from the contracted-vs-no-contract requirement for the same reason.
    """
    return _COMPOSER_CONTRACTS.get(template_id)


__all__ = [
    "NO_CONTRACT",
    "RUNTIME_ONLY_TEMPLATE_IDS",
    "ComposerContract",
    "_HUMANIZE_RESULT_ACTION_ALLOWED_KEYS",
    "_HUMANIZE_RESULT_ALLOWED_TOP_KEYS",
    "_is_full_ref",
    "get_composer_contract",
    "validate_humanize_result_payload",
]
