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
#
# ⚠️ Do NOT import from ``sreda.runtime.planner.*`` here (e.g. interpolation's
# ``contains_ref``). Even though ``interpolation.py`` itself is a pure leaf,
# importing it executes the ``sreda.runtime.planner`` PACKAGE __init__ first,
# which pulls in ``validator``, which imports this module while it is only
# partially initialized → ImportError. ${...}-ref detection for the new
# {step_id} form is therefore done LOCALLY below (Codex Phase 4a R2 CRITICAL —
# a fresh ``import sreda.services.composer_contracts`` broke on this exact cycle).
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

# Broad ${...}-token detector (embedded OR whole-string). Intentionally LOCAL
# (not ``interpolation.contains_ref``) to avoid the runtime.planner import cycle
# noted above. Broader than the planner's strict ref grammar — for a recognition
# GATE that is the safe direction: anything ${...}-looking in a new-form intent
# → reject → fall through to the old resolve_refs path (never feed an unresolved
# ref to the LLM). ``[^}]*`` (zero-or-more) so even the EMPTY malformed token
# ``${}`` is caught (Codex Phase 4a R3 MAJOR medium — ``[^}]+`` missed it).
# Both the contract and the normalizer share THIS detector via
# ``is_step_id_narration_form``, so they agree on what "the new form" is.
_EMBEDDED_REF_RE = re.compile(r"\$\{[^}]*\}")


def _is_full_ref(v: object) -> bool:
    """Return True if ``v`` is a string that is exactly one ``${...}`` ref.

    Used at plan-validation time (allow_refs=True) to defer checks on
    values that will only be resolved post-execution.
    """
    return isinstance(v, str) and _FULL_REF_RE.match(v) is not None


def is_step_id_action_item(item: object) -> bool:
    """True iff ``item`` is the NEW ``humanize_result`` narration form
    ``{"step_id": "s1"}`` — a dict whose ONLY key is ``step_id`` with a
    non-empty string value.

    #110 Phase 4: at plan-validation time the planner references a STEP; the
    composer's Layer-2 normalizer (#110 Phase 3) builds ``user_visible_summary``
    in code from that step's result post-execution. SINGLE SOURCE OF TRUTH for
    the new-form shape, shared by the contract here and the compose-time
    normalizer (``compose._is_step_id_action`` delegates to this). Valid ONLY
    when ``allow_refs=True`` (plan time); the strict runtime contract
    (``allow_refs=False``, validating the NORMALIZED output) requires the
    resolved ``{user_visible_summary, status}`` form and rejects ``step_id``.
    """
    return (
        isinstance(item, dict)
        and set(item.keys()) == {"step_id"}
        and isinstance(item.get("step_id"), str)
        and bool(item["step_id"].strip())
    )


def is_step_id_narration_form(data: object) -> bool:
    """True iff ``data`` is the COMPLETE new ``humanize_result`` narration form:
    a dict with top-level keys EXACTLY ``{intent, actions}``, ``intent`` a
    non-empty string with NO ``${...}`` reference (full OR embedded), and
    ``actions`` a non-empty list whose EVERY item is ``is_step_id_action_item``.

    #110 Phase 4 — SINGLE SOURCE OF TRUTH for "the new form", shared by the
    plan-time contract (``validate_humanize_result_payload``, allow_refs=True)
    and the compose-time normalizer (``compose._normalize_humanize_result``), so
    a plan is accepted at validation iff the normalizer will recognize it — no
    plan-valid/runtime-invalid gap (Codex Phase 4a R1 MAJOR). ${...}-ref detection
    is LOCAL (``_EMBEDDED_REF_RE``), kept out of ``interpolation`` to avoid the
    runtime.planner import cycle (Codex Phase 4a R2 CRITICAL)."""
    if (
        not isinstance(data, dict)
        or set(data.keys()) != _HUMANIZE_RESULT_ALLOWED_TOP_KEYS
    ):
        return False
    intent = data.get("intent")
    if (
        not isinstance(intent, str)
        or not intent.strip()
        or _EMBEDDED_REF_RE.search(intent) is not None
    ):
        return False
    actions = data.get("actions")
    if not isinstance(actions, list) or not actions:
        return False
    return all(is_step_id_action_item(item) for item in actions)


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

    # #110 Phase 4 — the NEW {step_id} narration form is ALL-OR-NOTHING and
    # accepted ONLY at plan time (allow_refs=True). If ANY action item is a
    # {step_id} item, the WHOLE payload must be the uniform new form (every action
    # {step_id}, 'intent' a non-empty literal with NO ${...} ref) — EXACTLY what
    # the composer's Layer-2 normalizer recognizes, so plan-valid ⟺ normalizer-
    # recognized (no plan-valid/runtime-invalid gap; Codex Phase 4a R1 MAJOR high).
    # The strict runtime contract (allow_refs=False) never takes this branch, so
    # {step_id} stays a disallowed action key there.
    if allow_refs and any(is_step_id_action_item(item) for item in actions):
        if not is_step_id_narration_form(data):
            errors.append(
                "humanize_result: the new {step_id} narration form must be uniform "
                "— every action must be {step_id} and 'intent' a non-empty literal "
                "string with no ${...} reference."
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


# ---------------------------------------------------------------------------
# #118 — required-keys contracts for data-carrying templates
# ---------------------------------------------------------------------------
# The original classification marked these NO_CONTRACT («no leak or crash
# surface beyond what schema + StrictUndefined already cover») — but the
# StrictUndefined crash IS the failure mode: a plan that picks the template
# without binding its variables passes validation, then dies at render time
# and degrades to the ``partial_with_compose_error`` stub. Seen live on the
# #115 r3 replay (199 real turns of tenant_tg_755682022): 6 turns hit
# «'items' is undefined». A plan-time completeness check turns that into a
# ``composer_contract_invalid`` violation, which flows into the orchestrator's
# one-shot retry feedback — the planner self-corrects instead of stubbing.

TEMPLATE_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "shopping_added_ok": ("items",),
    "shopping_added_empty": ("duplicates",),
    "shopping_list_show": ("count", "items"),
    "reminder_set_ok": ("when_phrase", "what"),
    "reminder_skipped_past": ("trigger_at_local", "late_by_minutes"),
    "reminders_list_show": ("count", "items"),
    "checklist_show": ("title", "items"),
    "checklist_empty": ("title",),
    "recipe_show": ("recipe_text",),
    "recipe_not_found_ask_alt": ("query",),
    "ask_when_to_remind": ("what",),
}
"""Per-template variables the Jinja source renders UNGUARDED (no ``is
defined``): absent → guaranteed StrictUndefined crash at render time. The
anti-drift test (test_118_template_contracts) parses each template and fails
when a template gains a variable not covered here or in
``TEMPLATE_OPTIONAL_KEYS``."""

TEMPLATE_OPTIONAL_KEYS: dict[str, tuple[str, ...]] = {
    # guarded with ``is defined`` in the template source — legal to omit
    "ask_user_for_clarification": ("missing_fields",),
    "partial_with_clarification": ("done_summary", "missing_fields"),
    "partial_with_compose_error": ("execution_summary",),
    "invalid_plan_fallback": ("attempt_count",),
}

# RUNTIME-ONLY templates (never planner-emitted; the validator rejects them
# upstream as ``runtime_only_template_in_plan``; the runtime producer —
# selector.py — guarantees the payload). Accounted separately from
# TEMPLATE_OPTIONAL_KEYS so render-required runtime variables (``count``)
# don't masquerade as "optional" in the anti-drift vocabulary (Codex #118 R1).
RUNTIME_ONLY_TEMPLATE_KEYS: dict[str, tuple[str, ...]] = {
    "selector_ambiguity_clarification": (
        "count", "partial", "done_summary", "options",
    ),
}

# Per-item fields the list templates render UNGUARDED on each element of a
# LITERAL ``items`` list (``it.raw_line`` / ``it.title`` / ``it.item_status``).
# Verified empirically: a str element or a dict missing the field crashes
# StrictUndefined at render (Codex #118 R1 MAJOR — top-level checks alone
# leave the validate-pass/render-fail class open one level deeper).
_TEMPLATE_ITEM_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "shopping_list_show": {"items": ("raw_line",)},
    "reminders_list_show": {"items": ("raw_line",)},
    "checklist_show": {"items": ("title", "item_status")},
}


def _make_required_keys_contract(
    template_id: str,
    required: tuple[str, ...],
    item_fields: dict[str, tuple[str, ...]] | None = None,
) -> "ComposerContract":
    """Build a completeness contract: every ``required`` key must be present
    and non-blank (a ``${sN.field}`` ref counts as present at plan time).

    ``item_fields`` (Codex #118 R1 MAJOR): for list templates, each element
    of a LITERAL list value must be an object carrying the per-item fields
    the template renders unguarded (``it.raw_line`` etc.) — a str element or
    a dict missing the field is a guaranteed StrictUndefined render crash.
    Full ``${sN.field}`` refs (the whole list, an element, or a field value)
    defer to post-execution resolution when ``allow_refs``."""

    def _blank(val: object) -> bool:
        # one predicate for BOTH levels (Codex #118 R2 medium MINOR —
        # nested values must reject empty []/{} too, not just None/whitespace)
        return (
            val is None
            or (isinstance(val, str) and not val.strip())
            or (isinstance(val, (list, dict)) and not val)
        )

    def _contract(data: dict, *, allow_refs: bool) -> list[str]:
        if not isinstance(data, dict):
            return [
                f"{template_id} template_data must be a dict, "
                f"got {type(data).__name__!r}."
            ]
        errors: list[str] = []
        for key in required:
            if key not in data:
                errors.append(
                    f"{template_id} template_data[{key!r}] is missing — the "
                    f"template cannot render without it. Bind a literal or a "
                    f"${{sN.field}} ref (e.g. from the step's output fields)."
                )
                continue
            val = data[key]
            if allow_refs and _is_full_ref(val):
                continue
            if _blank(val):
                errors.append(
                    f"{template_id} template_data[{key!r}] is blank "
                    f"({val!r}) — would render an empty/meaningless reply."
                )
        for list_key, fields in (item_fields or {}).items():
            val = data.get(list_key)
            if val is None or (allow_refs and _is_full_ref(val)):
                continue  # missing/blank already flagged above; ref deferred
            if not isinstance(val, list):
                errors.append(
                    f"{template_id} template_data[{list_key!r}] must be a "
                    f"list of objects or a ${{sN.field}} ref, got "
                    f"{type(val).__name__!r}."
                )
                continue
            for i, item in enumerate(val):
                if allow_refs and _is_full_ref(item):
                    continue
                if not isinstance(item, dict):
                    errors.append(
                        f"{template_id} template_data[{list_key!r}][{i}] must "
                        f"be an object with {sorted(fields)} (the template "
                        f"renders these per-item fields), got "
                        f"{type(item).__name__!r}."
                    )
                    continue
                for f in fields:
                    if f not in item:
                        errors.append(
                            f"{template_id} template_data[{list_key!r}][{i}]"
                            f"[{f!r}] is missing — the template renders it "
                            f"and crashes without it."
                        )
                        continue
                    fv = item[f]
                    if allow_refs and _is_full_ref(fv):
                        continue
                    if _blank(fv):
                        errors.append(
                            f"{template_id} template_data[{list_key!r}][{i}]"
                            f"[{f!r}] is blank ({fv!r})."
                        )
        return errors

    return _contract


# Render samples — one per template in HOUSEWIFE_TEMPLATES. Single source of
# truth for the registry render test (every template renders on its sample
# without StrictUndefined) AND contract self-consistency (each sample passes
# its own contract). Values are realistic, PII-free.
SAMPLE_TEMPLATE_DATA: dict[str, dict] = {
    "shopping_added_ok": {"items": ["молоко", "хлеб"]},
    "shopping_added_empty": {"duplicates": ["молоко"]},
    "shopping_list_show": {
        "count": 2,
        "items": [{"raw_line": "молоко"}, {"raw_line": "хлеб (2 шт)"}],
    },
    "shopping_list_empty": {},
    "reminder_set_ok": {"when_phrase": "завтра в 8:00", "what": "отправить образцы"},
    "reminder_skipped_past": {
        "trigger_at_local": "сегодня 09:00", "late_by_minutes": 42,
    },
    "reminders_list_show": {
        "count": 1, "items": [{"raw_line": "завтра 08:00 — образцы"}],
    },
    "reminders_list_empty": {},
    "checklist_show": {
        "title": "Дача",
        "items": [
            {"title": "грабли", "item_status": "pending"},
            {"title": "перчатки", "item_status": "done"},
        ],
    },
    "checklist_empty": {"title": "Дача"},
    "recipe_show": {"recipe_text": "Борщ: свёкла, капуста, говядина."},
    "recipe_not_found_ask_alt": {"query": "борщ"},
    "ask_user_for_clarification": {"missing_fields": ["time"]},
    "ask_when_to_remind": {"what": "позвонить сантехнику"},
    "partial_with_clarification": {
        "done_summary": "добавила молоко", "missing_fields": ["time"],
    },
    "selector_ambiguity_clarification": {"count": 2, "options": ["Дача", "Дом"]},
    "generic_tool_error": {},
    "partial_with_compose_error": {"execution_summary": "add_checklist_items"},
    "invalid_plan_fallback": {"attempt_count": 2},
    "identity_playful": {},
    "smalltalk_fallback": {},
}


# Authoritative classification of every planner-exposed ``template_id`` in
# ``HOUSEWIFE_TEMPLATES`` (services/composer/templates_housewife.py). Keep in
# sync: the coverage test fails if the composer registry grows a template that
# is neither here nor in RUNTIME_ONLY_TEMPLATE_IDS.
_COMPOSER_CONTRACTS: dict[str, ComposerContract | object] = {
    # ---- real contracts ----------------------------------------------------
    # Clarification family — referenced from the leaf, not duplicated.
    "ask_user_for_clarification": validate_clarification_payload,
    "partial_with_clarification": validate_clarification_payload,
    # ---- required-keys contracts (#118) -------------------------------------
    # Data-carrying templates: a plan that picks them MUST bind the unguarded
    # variables, or render is a guaranteed StrictUndefined crash → stub reply.
    # Built from TEMPLATE_REQUIRED_KEYS (one source of truth with the
    # anti-drift test in test_118_template_contracts.py).
    **{
        tid: _make_required_keys_contract(
            tid, keys, _TEMPLATE_ITEM_FIELDS.get(tid)
        )
        for tid, keys in TEMPLATE_REQUIRED_KEYS.items()
    },
    # ---- explicit "no contract" --------------------------------------------
    # Templates with no variables at all, or only ``is defined``-guarded
    # optional ones (see TEMPLATE_OPTIONAL_KEYS).
    "shopping_list_empty": NO_CONTRACT,
    "reminders_list_empty": NO_CONTRACT,
    # error / fallback — fixed canned text, error_code never rendered;
    # execution_summary / attempt_count are optional-guarded.
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
    "RUNTIME_ONLY_TEMPLATE_KEYS",
    "SAMPLE_TEMPLATE_DATA",
    "TEMPLATE_OPTIONAL_KEYS",
    "TEMPLATE_REQUIRED_KEYS",
    "ComposerContract",
    "_HUMANIZE_RESULT_ACTION_ALLOWED_KEYS",
    "_HUMANIZE_RESULT_ALLOWED_TOP_KEYS",
    "_is_full_ref",
    "get_composer_contract",
    "validate_humanize_result_payload",
]
