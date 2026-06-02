"""Shared key-allowlist constants and validation logic for ``humanize_result``
LLM prompt data.

Kept in a standalone module with zero intra-project imports so that both
``sreda.services.composer.prompts_registry`` (services layer) and
``sreda.runtime.planner.validator`` (planner layer) can import from here
without creating a circular dependency.

The cycle that motivated extraction:
  validator → composer/__init__ → compose → executor → plan_compiler → validator

Placing the constants here (``sreda.services.composer_contracts``) avoids
triggering ``sreda.services.composer.__init__`` on import, because the
module is a sibling of the ``composer`` package directory, not inside it.
"""

from __future__ import annotations

import re

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


__all__ = [
    "_HUMANIZE_RESULT_ACTION_ALLOWED_KEYS",
    "_HUMANIZE_RESULT_ALLOWED_TOP_KEYS",
    "_is_full_ref",
    "validate_humanize_result_payload",
]
