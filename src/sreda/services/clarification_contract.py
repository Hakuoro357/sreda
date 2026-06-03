"""Shared constants and validation logic for ``needs_clarification`` template data.

Kept in a standalone module with zero intra-project imports so that both
``sreda.services.composer.registry`` (services layer) and
``sreda.runtime.planner.schemas`` (planner layer) can import from here
without creating a circular dependency.

The cycle that motivated extraction:
  schemas → composer/__init__ → compose → executor → plan_compiler → schemas

Placing the constants here (``sreda.services.clarification_contract``) avoids
triggering ``sreda.services.composer.__init__`` on import, because the
module is a sibling of the ``composer`` package directory, not inside it.
"""

from __future__ import annotations

import re

# Matches ANY ``${...}`` token anywhere in a string (not only a full-string
# ref). ``done_summary`` must be FULLY literal: a mixed value like
# ``"сделала: ${s1.result}"`` would otherwise pass a full-ref-only check, then
# get its embedded ref resolved + stringified (dict/list → raw object) into
# the user reply. Codex PR-a R2 (both reviewers). Local regex keeps this
# module zero-intra-project-import (no interpolation.contains_ref dependency).
_CONTAINS_REF_RE = re.compile(r"\$\{[^}]*\}")


CLARIFICATION_FIELDS: frozenset[str] = frozenset({
    "reminder_subject",
    "time",
    "date",
    "recipient",
    "items",
    "quantity",
    "recipe_name",
    "query",
    "details",
})
"""Closed enum of field codes the planner may emit in ``missing_fields``.

Single source of truth — both the Jinja filter and the plan-time validator
reference this set. Any code not in this set is rejected by
``validate_clarification_payload`` and never echoed raw to the user.
"""


CLARIFICATION_FIELD_RU: dict[str, str] = {
    "reminder_subject": "что напомнить",
    "time": "когда (сегодня, завтра, дата + время)",
    "date": "какая дата",
    "recipient": "кому напомнить (тебе или другому)",
    "items": "что именно (несколько слов)",
    "quantity": "сколько (количество или объём)",
    "recipe_name": "название блюда",
    "query": "что искать",
    "details": "",  # empty → falls back to GENERIC_CLARIFICATION at render time
}
"""Exhaustive map of field code → short Russian question fragment.

Voice: на «ты», terse, matching template style.
Every code in CLARIFICATION_FIELDS must have an entry here.
An empty string signals "use GENERIC_CLARIFICATION" — see ``clarification_field_ru``.
"""

# Verify exhaustiveness at import time (cheap, no deps).
assert set(CLARIFICATION_FIELD_RU.keys()) == CLARIFICATION_FIELDS, (
    f"CLARIFICATION_FIELD_RU must cover all CLARIFICATION_FIELDS. "
    f"Missing: {CLARIFICATION_FIELDS - set(CLARIFICATION_FIELD_RU.keys())}. "
    f"Extra: {set(CLARIFICATION_FIELD_RU.keys()) - CLARIFICATION_FIELDS}."
)


GENERIC_CLARIFICATION: str = "Уточни, пожалуйста, детали запроса."
"""Fallback phrase when a field code is unknown or maps to an empty RU string."""

# slots deferred to follow-up (#TBD) — see plan Piece-1 Alternatives


def clarification_field_ru(code: str) -> str:
    """Return the Russian question fragment for a missing-field code.

    Returns ``CLARIFICATION_FIELD_RU[code]`` when known AND non-empty;
    otherwise returns ``GENERIC_CLARIFICATION``. Never echoes the raw code
    to the user — defense-in-depth even though validation rejects unknown
    codes upstream.

    Used as a Jinja filter (``| clarify_ru``) in the clarification templates.
    """
    fragment = CLARIFICATION_FIELD_RU.get(code, "")
    return fragment if fragment else GENERIC_CLARIFICATION


def validate_clarification_payload(
    data: dict,  # type: ignore[type-arg]
    *,
    allow_refs: bool,
) -> list[str]:
    """Validate the ``needs_clarification`` template_data payload.

    Single source of truth used by BOTH:
    - ``sreda.runtime.planner.validator._check_composer_allowlist``
      (plan-time, ``allow_refs=True`` — values may still be unresolved
      ``${...}`` refs, covers root + all branch composes).
    - (future) runtime composer path (``allow_refs=False``).

    Returns a list of human-readable error strings; an EMPTY list means
    the payload is valid.

    Rules
    -----
    Top-level keys
        Must be a subset of ``{missing_fields, done_summary, clarity_reason}``.
        Extra keys are always rejected.

    ``missing_fields`` (optional)
        Must be a ``list``; each element a ``str`` in ``CLARIFICATION_FIELDS``.
        Unknown code → error. Empty list is allowed (template has generic
        fallback). Non-list → error.

    ``done_summary`` (optional)
        Non-empty str that is NOT a full-ref (``"${...}"``). Refs are
        rejected regardless of ``allow_refs`` — a ref could resolve to a
        dict/list and get stringified raw into the user reply. Must be a
        literal string so the composer can render it safely.
    """
    errors: list[str] = []

    # ``clarity_reason`` is auto-merged into template_data by the schema
    # validator (Plan._validate_clarity) so the Jinja template can render
    # it if needed. It is a passthrough field — we validate only the
    # structured clarification-specific keys here.
    _ALLOWED_TOP_KEYS: frozenset[str] = frozenset({
        "missing_fields", "done_summary", "clarity_reason",
    })

    # ---- top-level key allowlist -----------------------------------------
    extra_top = sorted(set(data.keys()) - _ALLOWED_TOP_KEYS)
    if extra_top:
        errors.append(
            f"clarification template_data has disallowed top-level keys: "
            f"{extra_top}. Only {sorted(_ALLOWED_TOP_KEYS)} are permitted."
        )

    # ---- missing_fields --------------------------------------------------
    missing_fields = data.get("missing_fields")
    if missing_fields is not None:
        if not isinstance(missing_fields, (list, tuple)):
            errors.append(
                f"clarification template_data['missing_fields'] must be a list, "
                f"got {type(missing_fields).__name__!r}."
            )
        else:
            for i, code in enumerate(missing_fields):
                if not isinstance(code, str):
                    errors.append(
                        f"clarification template_data['missing_fields'][{i}] "
                        f"must be a str, got {type(code).__name__!r}."
                    )
                elif code not in CLARIFICATION_FIELDS:
                    errors.append(
                        f"clarification template_data['missing_fields'][{i}]={code!r} "
                        f"is not a known clarification field code. "
                        f"Known codes: {sorted(CLARIFICATION_FIELDS)}."
                    )

    # ---- done_summary ----------------------------------------------------
    # Must be a literal non-empty string. Full-ref strings (${...}) are
    # ALWAYS rejected regardless of allow_refs — a ref can resolve to a
    # dict/list which would be stringified raw into the user reply.
    done_summary = data.get("done_summary")
    if done_summary is not None:
        if isinstance(done_summary, str) and _CONTAINS_REF_RE.search(done_summary):
            errors.append(
                f"clarification template_data['done_summary']={done_summary!r} "
                f"contains a ${{...}} ref. done_summary must be a FULLY literal "
                f"string — no refs anywhere (not even embedded in text), because "
                f"a ref can resolve to a dict/list and get stringified raw into "
                f"the user reply."
            )
        elif not isinstance(done_summary, str) or not done_summary.strip():
            errors.append(
                f"clarification template_data['done_summary'] must be a non-empty "
                f"string, got {done_summary!r}."
            )

    return errors


__all__ = [
    "CLARIFICATION_FIELD_RU",
    "CLARIFICATION_FIELDS",
    "GENERIC_CLARIFICATION",
    "clarification_field_ru",
    "validate_clarification_payload",
]
