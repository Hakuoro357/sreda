"""Frozen gold-label expectations manifest for the offline replay harness (#87).

``ReplayCase`` and its nested ``ReplayExpectations`` encode the per-turn
gold labels that are FROZEN before replay starts (plan §4.4 — kills Q-A/Q-B
circularity).  Labels describe user-visible outcomes and intents, NOT the
new planner's action vocabulary.

PII split (§4.4 / §15):
    This module lives in the PUBLIC ``sreda`` repo.  It holds ONLY redacted
    structural labels — opaque turn-id hashes, intent codes, domain-action
    codes.  Raw user text, ``exactness_rule`` payloads, and payload-match
    values live ONLY in the local quarantine export (outside the repo).

Memory eligibility predicate (R5 fix — §4.4 / §12):
    ``memory_eligible := (memory_dependence == "independent")
                         OR (memory_dependence == "dependent"
                             AND current_memory_status == "valid")``

    For memory-INDEPENDENT turns ``current_memory_status`` should be
    ``"not_applicable"``; the predicate ignores it and marks the turn
    eligible.  Only memory-DEPENDENT turns with status
    ``stale | missing | contradicted | unknown`` are excluded from the
    headline verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Sub-types
# ---------------------------------------------------------------------------

ExpectedIntent = Literal["read", "write", "mixed", "smalltalk", "howto"]

MemoryDependence = Literal["independent", "dependent"]

MemoryStatus = Literal[
    "valid", "stale", "missing", "contradicted", "unknown", "not_applicable"
]

LabelSource = Literal["manual", "classifier"]


@dataclass(frozen=True)
class ReplayExpectations:
    """Gold-label expectations for one ``ReplayCase`` (§4.4).

    All fields are frozen before replay; modifying them after seeing
    results invalidates pre-registration (§12).

    Fields
    ------
    expected_intent :
        Broad intent class for the turn.  Used by invariant #2
        (unsolicited-write) and strategy classification.
    required_outcomes :
        User-visible outcomes a correct response MUST achieve (domain /
        outcome level, NOT planner action names).
    acceptable_action_sets :
        List of acceptable domain-action sets.  Each inner list is one
        correct path through the tool taxonomy.  At least one must be
        satisfied by BOTH the old and new runs to pass invariant #4.
        Empty outer list means "any or no action is acceptable"
        (e.g. smalltalk turns).
    allowed_write_targets :
        Domain write targets that are explicitly allowed.  Empty for pure
        read turns.
    forbidden_write_targets :
        Domain write targets that MUST NOT be written.  Invariant #2
        checks new-run planned writes against this list.
    forbidden_actions :
        Specific domain-actions that must NOT appear in the plan.
    requires_clarification :
        True when the correct response is to ask for clarification (not
        silently act).  Drives invariant #5 (atomicity).
    exactness_rule :
        Human-readable rule for invariant #4 grading at the OUTCOME level
        (stored in quarantine for PII; repo copy is a redacted placeholder
        or empty string).
    memory_dependence :
        Whether a correct answer requires stored preferences / memories.
    current_memory_status :
        State of the relevant memory entries at replay time.  Must be
        ``"not_applicable"`` for memory-independent turns.
    eligibility_reason :
        Short human-readable rationale for the memory-eligibility decision.
    must_cover :
        True for named headline-incident turns that MUST be replayed; any
        ``must_cover=True`` turn that ends up ``unknown`` after
        reconstruction triggers a Stage-0 ``NO DECISION`` (§12).
    incident_id :
        Provenance string (e.g. ``"#59"`` or ``"phantom-save-incident-3"``)
        for ``must_cover`` turns.
    label_source :
        Who produced this label.  Headline incident suite MUST be
        ``"manual"``; broad smoke may be ``"classifier"``.
    labeler :
        Identifier of the human or classifier that produced the label.
    confidence :
        Label confidence in [0.0, 1.0].  1.0 for carefully reviewed
        manual labels; lower for classifier outputs.
    """

    # --- intent / outcome ---
    expected_intent: ExpectedIntent
    required_outcomes: tuple[str, ...] = field(default_factory=tuple)
    acceptable_action_sets: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    allowed_write_targets: tuple[str, ...] = field(default_factory=tuple)
    forbidden_write_targets: tuple[str, ...] = field(default_factory=tuple)
    forbidden_actions: tuple[str, ...] = field(default_factory=tuple)
    requires_clarification: bool = False
    exactness_rule: str = ""  # redacted in repo; full text in quarantine

    # --- memory eligibility (frozen before replay, R3/R5) ---
    memory_dependence: MemoryDependence = "independent"
    current_memory_status: MemoryStatus = "not_applicable"
    eligibility_reason: str = ""

    # --- must-cover obligation (R4) ---
    must_cover: bool = False
    incident_id: str = ""

    # --- provenance ---
    label_source: LabelSource = "manual"
    labeler: str = ""
    confidence: float = 1.0

    # ------------------------------------------------------------------
    # Derived predicate (§4.4 / §12, R5 fix)
    # ------------------------------------------------------------------

    @property
    def memory_eligible(self) -> bool:
        """True when this turn is eligible for the headline verdict.

        Predicate (R5 — fixes over-filtering of independent turns):
            independent → always eligible (current_memory_status ignored)
            dependent   → eligible ONLY when current_memory_status == "valid"

        Memory-DEPENDENT turns with stale/missing/contradicted/unknown
        memory are excluded from the headline denominator (counted in the
        attrition audit).
        """
        if self.memory_dependence == "independent":
            return True
        # dependent branch
        return self.current_memory_status == "valid"


@dataclass(frozen=True)
class ReplayCase:
    """One archived turn ready for offline replay.

    PII fields (turn_id in its opaque hash form, tenant placeholder) are
    safe for the public repo.  Raw user text and history live only in the
    local quarantine JSONL export.

    Attributes
    ----------
    turn_id_hash :
        Opaque SHA-256 hex prefix of the real turn id (first 16 chars).
        Used for stable correlation without exposing the raw id.
    tenant_placeholder :
        Redacted tenant identifier safe for the repo
        (e.g. ``"tenant_tg_<SMOKE>"``).
    expectations :
        Frozen gold-label expectations labeled BEFORE replay.
    reconstruction_status :
        Per-field reconstruction fidelity results populated AFTER the
        export + reconstruct phase (Phase B).  None until reconstruction
        runs.  Stored alongside the case so scoring can gate on it.
    """

    turn_id_hash: str
    tenant_placeholder: str
    expectations: ReplayExpectations
    reconstruction_status: dict[str, str] | None = None  # None until Phase B

    @property
    def is_memory_eligible(self) -> bool:
        """Shortcut to ``expectations.memory_eligible``."""
        return self.expectations.memory_eligible


__all__ = [
    "ExpectedIntent",
    "LabelSource",
    "MemoryDependence",
    "MemoryStatus",
    "ReplayCase",
    "ReplayExpectations",
]
