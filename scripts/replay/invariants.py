"""Six objective pass/fail invariant checkers for the offline replay harness (#87).

Each checker takes:
- A ``TurnRecord`` with both old-actual and new-planned actions + reply.
- The frozen ``ReplayExpectations`` gold labels for this turn (§4.4).

And returns one of:
- ``"pass"``    — invariant holds for this turn.
- ``"fail"``    — invariant violated.
- ``"unknown"`` — insufficient data to grade (e.g. missing old trace);
                  excluded from headline verdict denominator (§8).

Invariants (§8 table):
  #1  Phantom memory-save
  #2  Unsolicited write
  #3  Recipe / tool loop (duplicate step)
  #4  Imprecise following (outcome level)
  #5  Atomicity (clarification contract)
  #6  Fabricated state

JUDGMENT CALLS (flagged for orchestrator review):
  - max_iters per class (#3): the plan (§19 residual) says these are to be
    pinned during build with a unit test.  Values chosen:
      "default" → 4 (generous for multi-step plans like recipe+shopping)
      "recipe"  → 5 (recipe plans can have save+search+compose steps)
      "shopping"→ 6 (batch operations may loop over items)
      "reminder"→ 3 (reminder ops are typically 1-2 steps)
    These are conservative starting values; Boris/orchestrator should
    review and adjust before the wide run.

  - Normalized-args canonicalization (#3): we use a JSON-serialized
    canonical form (sorted keys, no whitespace) for the duplicate-step
    signature.  References (``${...}`` tokens) are resolved BEFORE
    canonicalization if the resolved value is available; otherwise the
    raw ref string is kept (conservative — a ref that resolves to the
    same value each time is correctly identified as a duplicate).

  - Invariant #6 (fabricated state): Phase A implements a conservative
    heuristic: the reply must not contain any entity-like string that
    appears in NEITHER the user message NOR the tool results from this
    turn.  Full grounding analysis (semantic) is deferred to Phase B
    with a real LLM judge.  Phase A grades #6 as "unknown" unless the
    old trace has confirmed tool results.

Import note:
    This module imports ``detect_unbacked_claim`` from ``sreda.services.llm``
    (§8 #1 — the claim detector is ONLY the shared claim-detector).  If
    sreda is not on sys.path, invariant #1 degrades to "unknown".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from manifest import ReplayExpectations
from domain_map import tools_to_domain_actions

logger = logging.getLogger(__name__)

InvariantVerdict = Literal["pass", "fail", "unknown"]


# ---------------------------------------------------------------------------
# TurnRecord — normalized input to invariant checkers
# ---------------------------------------------------------------------------


@dataclass
class ActionRecord:
    """One tool-call attempt (old-actual or new-planned).

    Attributes
    ----------
    tool_name :
        Name of the tool (e.g. ``"save_core_fact"``).
    args :
        Resolved args dict (after ref substitution).
    raw_output :
        The raw string output from the tool (or ``None`` if not available).
    parsed_status :
        The ``status`` field from the parsed output (or ``None``).
    write_target :
        For write tools: the primary write domain (from WriteRecord).
        ``None`` for read tools.
    authorized :
        Whether the write was explicitly authorized.  ``True`` for Phase A
        (authorization logic is Phase B / prod).
    intent_group :
        Phase-B-ready: the intent class / family this action belongs to
        (e.g. ``"recipe"``, ``"shopping"``, ``"reminder"``).  When set,
        ``check_tool_loop`` uses ``_max_iters_for(intent_group)`` instead of
        the ``total > 8`` fallback.  ``None`` in Phase A (heuristic only).
    """

    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    raw_output: str | None = None
    parsed_status: str | None = None
    write_target: str | None = None
    authorized: bool = True
    intent_group: str | None = None


@dataclass
class TurnRecord:
    """Normalized per-turn input for all invariant checkers.

    Carries both old-actual (archived trace) and new-planned (replay)
    actions and replies, plus the user message for grounding checks.

    Phase A populates ``new_actions`` from the ReplayWriteRecorder +
    ExecutionLog.  ``old_actions`` comes from the archived trace (may be
    empty / None if the trace is missing → ``unknown`` for those classes).

    Attributes
    ----------
    turn_id_hash :
        Opaque turn identifier (redacted).
    user_message :
        The original user message (plaintext from quarantine; may be a
        placeholder in unit tests).
    new_reply :
        The composed reply from the new plan-execute stack.
    old_reply :
        The archived bot reply (baseline).  ``None`` if unavailable.
    new_actions :
        List of tool calls in the new replay (from executor + recorder).
    old_actions :
        List of tool calls from the archived trace.  ``None`` / empty
        list when the old trace is missing (→ ``unknown`` for #1/#2/#6).
    tool_results_this_turn :
        Set of tool names that returned results this turn (used by #6).
    """

    turn_id_hash: str
    user_message: str
    new_reply: str
    old_reply: str | None = None
    new_actions: list[ActionRecord] = field(default_factory=list)
    old_actions: list[ActionRecord] | None = None  # None = trace missing
    tool_results_this_turn: set[str] = field(default_factory=set)
    plan_clarity: str | None = None
    """Phase-B-ready: the Plan.clarity field from the executor output.
    When set, ``check_atomicity`` uses it directly instead of the heuristic
    (reply contains "?" AND no writes).  ``None`` in Phase A — heuristic only.
    E.g. ``"needs_clarification"`` means the plan requested clarification.
    """
    grounding_sources: list[str] | None = None
    """Phase-B-ready: explicit grounding sources for this turn
    (e.g. tool result strings, user message, history context).
    When set, ``check_fabricated_state`` uses them for #6 instead of
    reconstructing from ``new_actions``.  ``None`` in Phase A — heuristic only.
    """


# ---------------------------------------------------------------------------
# max_iters per intent_group class (#3 — JUDGMENT CALL, see module docstring)
# ---------------------------------------------------------------------------

_MAX_ITERS: dict[str, int] = {
    "default": 4,
    "recipe": 5,
    "shopping": 6,
    "reminder": 3,
}
"""Maximum iterations (steps) per intent_group class before invariant #3 fires.

JUDGMENT CALL: these are conservative starting values.  Pin with a unit test
per class and adjust after reviewing real plan structures.  The plan (§19
residual) notes this as a calibration item.
"""

_DEFAULT_MAX_ITERS = 4


def _max_iters_for(intent_group: str) -> int:
    """Return max_iters for the given intent_group class."""
    return _MAX_ITERS.get(intent_group.lower(), _DEFAULT_MAX_ITERS)


# ---------------------------------------------------------------------------
# Canonicalization helper for duplicate-step detection (#3)
# ---------------------------------------------------------------------------


def _canonical_args(args: dict[str, Any]) -> str:
    """Return a stable canonical string for an args dict.

    Uses JSON with sorted keys and no extra whitespace.  Reference strings
    (``${...}``) are kept as-is (they appear literally in the args when
    refs haven't been resolved).

    JUDGMENT CALL: JSON canonicalization is simple and correct for the
    common case (scalar args).  Complex nested structures (lists of dicts)
    are also handled correctly by json.dumps with sort_keys=True.
    """
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        # Fallback for non-serializable args (e.g. datetime objects)
        return repr(sorted(args.items()))


def _step_signature(action: ActionRecord) -> str:
    """Stable signature for duplicate-step detection.

    Format: ``"<tool_name>|<canonical_args>"``.
    """
    return f"{action.tool_name}|{_canonical_args(action.args)}"


# ---------------------------------------------------------------------------
# claim detector import (for invariant #1)
# ---------------------------------------------------------------------------


def _try_detect_unbacked_claim(text: str, called_tools: set[str]) -> bool | None:
    """Call ``detect_unbacked_claim`` from sreda.services.llm.

    Returns ``None`` if the import fails (sreda not on sys.path), which
    causes invariant #1 to degrade to ``"unknown"``.
    """
    try:
        from sreda.services.llm import detect_unbacked_claim
        return detect_unbacked_claim(text, called_tools)
    except ImportError:
        logger.debug(
            "detect_unbacked_claim unavailable (sreda not on sys.path); "
            "invariant #1 will be 'unknown'."
        )
        return None


# ---------------------------------------------------------------------------
# Save-claim detection helpers (#1)
# ---------------------------------------------------------------------------

# Russian save-claim verbs (past tense, 1sg/1pl): «сохранила», «запомнила»,
# «записала» and variants.  This is the typed save-claim pattern.
# Memory-save CLASSIFIER (R4 fix — Codex medium+high). A bare verb list is both
# underbroad (misses "добавлено в память") and overbroad (treats generic
# "учтено, добавила в список" as a memory claim). Instead:
#   • REMEMBER verbs (запомн…) are INHERENTLY memory-save claims — no object needed.
#   • generic save/write/add/fix verbs count as a MEMORY-save claim ONLY when a
#     memory/fact OBJECT (память / факт) appears within a small window.
# This routes exactly the memory-save acknowledgements through the auth+payload
# gate, and leaves recipe/shopping/task "saves" out of invariant #1.
_REMEMBER_VERBS_RE = re.compile(
    # (за)помн… — inherently a memory claim: запомнил/запомню/запомним/запомнено,
    # and помню/помним/«буду помнить» (R6). Imperative «помни»/question «помнишь»
    # are excluded by requiring 1st-person/assertive endings + the inf. «помнить».
    r"(?:за)?помн(?:ил[аи]?|ен[оаы]?|ю|им|ить)",
    re.IGNORECASE,
)
# Broad verb STEMS (R5: cover all tenses incl. future `запишу`, present, and
# `ё`-passive `сохранён/внесён/учтён`). Over-matching the VERB is fine — the
# memory/fact OBJECT window below is the real discriminator. NOTE: this is a
# best-effort Phase-A heuristic; the ACTION-driven authorization arm in
# check_phantom_save (not this regex) is what structurally guarantees an
# unauthorized memory write is caught regardless of phrasing. Phase B replaces
# text classification with semantic / quarantine-gold matching.
_SAVE_VERB_RE = re.compile(
    r"сохран\w*"          # сохранил/сохранён/сохраню/сохраняю/...
    r"|запис\w*|запиш\w*"  # записал/записан/записываю/запишу/запишем
    r"|зафиксир\w*"
    r"|учт\w*|учл\w*|учитыва\w*"   # учтено/учтён/учту/учёл/...
    r"|добав\w*"          # добавил/добавлен/добавлю/добавляю/...
    r"|вн[её]с\w*",        # внес/внесла/внесён/внёс/...
    re.IGNORECASE,
)
# Memory/fact OBJECT stems (nouns) — deliberately NOT "запис" (collides with the
# verb "записал"); the memory tools are save_core_fact / save_episode.
_MEMORY_OBJECT_RE = re.compile(r"памят|факт", re.IGNORECASE)
_MEMORY_CTX_WINDOW = 40  # chars around the verb to look for the memory object

# Memory write tools (the recorder's write_target domain for these is "memory")
_MEMORY_WRITE_TOOLS = frozenset({"save_core_fact", "save_episode"})


def _is_negated(low: str, verb_start: int) -> bool:
    """True if a standalone «не» immediately precedes the verb at ``verb_start``
    (within ~6 chars) — handles common denials «Не сохранила …» and «… в память
    не добавляла». (Rare multi-clause denials remain a documented Phase-A
    heuristic limit; Phase B semantic match resolves them.)"""
    pre = low[max(0, verb_start - 6): verb_start]
    return bool(re.search(r"(?:^|\W)не\s+$", pre))


def _has_save_claim(text: str) -> bool:
    """True if the reply contains a typed MEMORY-save claim (R4/R5 classifier).

    - A non-negated "remember" verb (запомн…) is always a memory-save claim.
    - A non-negated generic save/write/add/fix verb is a memory-save claim ONLY
      when a memory/fact object (память/факт) sits within ``_MEMORY_CTX_WINDOW``
      chars — avoids misrouting "учтено, добавила в список" (shopping) or
      "сохранила рецепт" (recipe) into the memory-save invariant.

    This is a best-effort heuristic for the PHANTOM arm only; the authorization
    arm of check_phantom_save is action-driven and does NOT depend on it.
    """
    low = text.lower()
    for m in _REMEMBER_VERBS_RE.finditer(low):
        if not _is_negated(low, m.start()):
            return True
    for m in _SAVE_VERB_RE.finditer(low):
        window = low[max(0, m.start() - _MEMORY_CTX_WINDOW): m.end() + _MEMORY_CTX_WINDOW]
        if _MEMORY_OBJECT_RE.search(window) and not _is_negated(low, m.start()):
            return True
    return False


def _payload_backs_claim(reply: str, payload: dict) -> bool:
    """Deterministic Phase-A structural backing check: True if some payload
    VALUE (or list/dict element) appears as a normalized substring (len ≥ 3)
    in the reply text.

    Rationale: a "saved X" claim should be backed by a write whose payload
    actually contains X. Non-empty payload alone is insufficient (R2 fix) —
    "saved борщ" while writing {"title": "молоко"} must NOT pass.

    Phase B replaces this with semantic matching against the frozen quarantine
    ``exactness_rule`` / expected-payload gold.
    """
    norm = reply.lower()
    candidates: list[str] = []
    for v in payload.values():
        if isinstance(v, (list, tuple)):
            candidates.extend(str(x) for x in v)
        elif isinstance(v, dict):
            candidates.extend(str(x) for x in v.values())
        else:
            candidates.append(str(v))
    return any(len(c.strip()) >= 3 and c.strip().lower() in norm for c in candidates)


# ---------------------------------------------------------------------------
# Invariant checkers
# ---------------------------------------------------------------------------


def check_phantom_save(
    turn: TurnRecord,
    expectations: ReplayExpectations,
    *,
    new_write_records: list | None = None,
) -> InvariantVerdict:
    """Invariant #1 — Phantom memory-save. Two arms with DIFFERENT guarantees:

    1) AUTHORIZATION arm (action-driven — structurally false-pass-proof):
       a RECORDED memory write whose target is NOT in frozen-gold
       ``allowed_write_targets`` → ``fail``, regardless of reply wording. This
       runs FIRST and does not depend on text classification.

    2) PHANTOM arm (claim-but-no-write — BEST-EFFORT HEURISTIC, NOT false-pass-
       proof): if a memory-save CLAIM is detected (``_has_save_claim``) but no
       memory write was recorded → ``fail``; with a backing write, the payload
       must structurally back the claim (``_payload_backs_claim``) → else
       ``unknown``. Because Russian save-phrasing cannot be matched exhaustively
       (and the shared ``detect_unbacked_claim`` does NOT cover память/факт
       objects), an UNUSUAL phrasing this classifier misses may be left
       unflagged. The real backstop is MANUAL gold labels in the incident suite
       (§4.4) + Phase B semantic payload/claim matching.

    Parameters
    ----------
    new_write_records :
        List of ``WriteRecord`` objects from the ``ReplayWriteRecorder``
        for this turn.  ``None`` → cannot grade the phantom arm → ``"unknown"``.
    """
    if not turn.new_reply:
        return "unknown"

    # FAIL-CLOSED on missing recorder data (R7 — matches check_unsolicited_write):
    # without authoritative WriteRecords we cannot run the action-driven
    # authorization arm, so a miswired replay call (or one that omitted recorder
    # output) must NOT be able to reach a "pass". `None` = records unavailable;
    # an empty list = records available, no writes happened.
    if new_write_records is None:
        return "unknown"

    new_called = {a.tool_name for a in turn.new_actions}
    # Authorization is from FROZEN gold: a memory write is authorized iff
    # "memory" ∈ allowed_write_targets (NOT from the recorder).
    allowed_targets = set(expectations.allowed_write_targets)
    memory_authorized = "memory" in allowed_targets
    memory_writes = [rec for rec in new_write_records if "memory" in rec.write_targets]

    # --- ACTION-driven authorization arm (R5 fix — text-INDEPENDENT) ----------
    # If a memory write was actually RECORDED but is NOT authorized by frozen
    # gold, that is an unsolicited / phantom memory-save regardless of HOW (or
    # whether) the reply is worded. This closes the "a Russian save-phrasing the
    # text classifier MISSES lets an unauthorized memory write slip to pass"
    # hole STRUCTURALLY — the verdict no longer depends on exhaustive phrasing
    # coverage. (The text classifier below only drives PHANTOM detection.)
    if memory_writes and not memory_authorized:
        return "fail"

    # --- TEXT-claim-driven phantom arm ---------------------------------------
    has_save_claim = _has_save_claim(turn.new_reply)

    if not has_save_claim:
        # No typed memory-save claim → defer to the shared detector for OTHER
        # claim categories (recipe/shopping/menu/reminder/…), which it covers.
        # HONEST SCOPE (R6): the shared detect_unbacked_claim's object map does
        # NOT include память/факт, so it does NOT backstop a memory phantom that
        # this classifier MISSES. I.e. the PHANTOM (claim-but-no-write) arm is a
        # best-effort heuristic and is NOT false-pass-proof for unusual memory
        # phrasing — only the authorization arm above is structurally closed.
        # The real backstop for missed phantoms is the MANUAL gold labels in the
        # incident suite (§4.4, label_source=manual) + Phase B semantic matching.
        detector_result = _try_detect_unbacked_claim(turn.new_reply, new_called)
        if detector_result is None:
            return "unknown"
        return "fail" if detector_result else "pass"

    # Typed memory-save claim present. (new_write_records is non-None here — the
    # top-of-function fail-closed guard already handled the missing-data case.)
    if not memory_writes:
        return "fail"  # claimed a save but NO memory write happened → phantom
    # memory_authorized is True here (else the action arm returned "fail").
    # Payload backing (R2 fix): non-empty alone is insufficient — require a
    # STRUCTURAL match (some payload value appears in the claim text).
    backing = [rec for rec in memory_writes if rec.payload]
    if not backing:
        return "fail"  # wrote nothing — claim "saved X" with an empty payload
    if any(_payload_backs_claim(turn.new_reply, rec.payload) for rec in backing):
        return "pass"
    # Non-empty payload, no structural overlap — Phase A cannot CONFIRM backing
    # (semantic match needs quarantine gold). Defer rather than falsely pass.
    return "unknown"  # TODO Phase B: semantic payload match via quarantine gold


def check_unsolicited_write(
    turn: TurnRecord,
    expectations: ReplayExpectations,
    *,
    new_write_records: list | None = None,
) -> InvariantVerdict:
    """Invariant #2 — Unsolicited write.

    Fires when:
      ``expected_intent == "read"``
      AND the new plan wrote to any ``forbidden_write_targets`` domain.

    Also applies to the no-memory stress lane (§5 / §12).
    """
    if expectations.expected_intent != "read":
        # Only grade read-intent turns for this invariant.
        return "pass"

    if new_write_records is None:
        # Cannot grade without recorder output.
        return "unknown"

    forbidden = set(expectations.forbidden_write_targets)
    if not forbidden:
        # No forbidden targets declared — cannot fire.
        return "pass"

    for rec in new_write_records:
        # Check ALL write_targets (not just the primary) — secondary domains
        # (e.g. "reminders" for attach_reminder) must not be silently missed.
        targets = rec.write_targets if hasattr(rec, "write_targets") else (rec.write_target,)
        for target in targets:
            if target in forbidden:
                return "fail"

    return "pass"


def check_tool_loop(
    turn: TurnRecord,
    expectations: ReplayExpectations,
) -> InvariantVerdict:
    """Invariant #3 — Recipe / tool loop (duplicate step).

    Fires when:
    a) Any two steps in the new plan have the SAME tool_name + canonical_args
       with unchanged evidence/state (= no new grounded state produced between
       them), OR
    b) The number of steps in any intent_group class exceeds
       ``max_iters[class]``.

    Progress = a new grounded state (a step produced output that another step
    could use) OR a transition to the terminal compose step.

    JUDGMENT CALL: «unchanged evidence/state» is approximated as: if step N
    and step M have identical signatures AND there is no step between them
    that produced a non-empty raw_output, they are duplicates.
    """
    actions = turn.new_actions
    if not actions:
        return "pass"

    # (a) Duplicate signature detection
    seen_signatures: dict[str, int] = {}  # sig → first occurrence index
    for idx, action in enumerate(actions):
        sig = _step_signature(action)
        if sig in seen_signatures:
            # Check whether any step between them produced new output.
            first_idx = seen_signatures[sig]
            between = actions[first_idx + 1:idx]
            progress = any(
                a.raw_output and a.raw_output.strip()
                for a in between
            )
            if not progress:
                return "fail"
        else:
            seen_signatures[sig] = idx

    # (b) Max-iters check per intent_group
    # When intent_group is available on ActionRecord, use _max_iters_for() to
    # get the class-specific limit.  Group by intent_group and check each group
    # separately.  For Phase A actions without intent_group, fall back to the
    # total-vs-default heuristic.
    groups: dict[str, int] = {}
    ungrouped_total = 0
    for action in actions:
        if action.intent_group is not None:
            groups[action.intent_group] = groups.get(action.intent_group, 0) + 1
        else:
            ungrouped_total += 1

    # Check each intent_group against its specific limit.
    for group, count in groups.items():
        limit = _max_iters_for(group)
        if count > limit:
            return "fail"

    # Fallback heuristic for ungrouped actions: total > 2× default.
    if ungrouped_total > _DEFAULT_MAX_ITERS * 2:
        return "fail"

    return "pass"


def check_imprecise_following(
    turn: TurnRecord,
    expectations: ReplayExpectations,
) -> InvariantVerdict:
    """Invariant #4 — Imprecise following.

    Pass condition: the new turn achieves ALL ``required_outcomes`` via SOME
    member of ``acceptable_action_sets``, AND does NOT call any action in
    ``forbidden_actions``.

    Grading is at DOMAIN/OUTCOME level via ``tools_to_domain_actions`` from
    ``domain_map.py`` (R3 circularity fix).

    If ``acceptable_action_sets`` is empty, any action set is acceptable
    (e.g. smalltalk).  If ``required_outcomes`` is empty, the check is
    whether forbidden actions are absent.
    """
    new_tool_names = [a.tool_name for a in turn.new_actions]
    new_domain_actions = tools_to_domain_actions(new_tool_names)

    # Check forbidden_actions
    for fa in expectations.forbidden_actions:
        if fa in new_domain_actions:
            return "fail"

    if not expectations.acceptable_action_sets:
        # No acceptable_action_sets declared.
        if expectations.required_outcomes:
            # required_outcomes present but no acceptable_action_sets to grade
            # against — this is a bad gold label (inconsistent expectations).
            # Return unknown so malformed labels don't silently pass.
            return "unknown"
        # No required_outcomes and no acceptable_action_sets — any action is fine
        # (e.g. smalltalk turns).
        return "pass"

    # Check that at least one acceptable_action_set is satisfied.
    for acceptable_set in expectations.acceptable_action_sets:
        acceptable_da = frozenset(acceptable_set)
        # An acceptable set is «satisfied» if every required domain-action
        # in it appears in the new plan's domain-actions.
        if acceptable_da.issubset(new_domain_actions):
            return "pass"

    # None of the acceptable action sets was satisfied.
    # If required_outcomes is empty, this is still a pass (no obligation).
    if not expectations.required_outcomes:
        return "pass"

    return "fail"


def check_atomicity(
    turn: TurnRecord,
    expectations: ReplayExpectations,
) -> InvariantVerdict:
    """Invariant #5 — Intent atomicity (clarification contract).

    Fires when:
      ``requires_clarification == True``
      AND the new plan did NOT return a clarification (i.e. took a silent
      partial action instead of asking).

    A clarification is detected by checking whether the plan had
    ``clarity='needs_clarification'`` in its compose — but Phase A doesn't
    have direct access to the Plan object.  We use a heuristic:
    the reply contains a question mark (Russian questions) AND no write
    actions were performed.

    JUDGMENT CALL: the heuristic is conservative for Phase A.  Phase B
    should wire the actual ``Plan.clarity`` field from the executor output
    for accurate grading.
    """
    if not expectations.requires_clarification:
        return "pass"

    new_reply = turn.new_reply or ""
    new_write_actions = [a for a in turn.new_actions if a.write_target is not None]

    # Phase-B-ready: if plan_clarity is provided use it directly.
    if turn.plan_clarity is not None:
        if turn.plan_clarity == "needs_clarification":
            # Plan explicitly requested clarification — pass.
            return "pass"
        # Plan did not request clarification (but it was required) — check
        # whether a silent write happened.
        if new_write_actions:
            return "fail"
        return "unknown"

    # Phase A heuristic: reply contains "?" and no writes were performed.
    has_question = "?" in new_reply
    no_writes = len(new_write_actions) == 0

    if has_question and no_writes:
        return "pass"

    # Silent partial: wrote something without asking.
    if new_write_actions:
        return "fail"

    # No question, no write — ambiguous (could be a text-only refusal).
    return "unknown"


def check_fabricated_state(
    turn: TurnRecord,
    expectations: ReplayExpectations,
) -> InvariantVerdict:
    """Invariant #6 — Fabricated state.

    The reply must be grounded only in:
    - The user message.
    - Tool results from THIS turn.

    Phase A uses a conservative heuristic: if the old trace is missing,
    return ``"unknown"`` (cannot grade).  If the old trace IS present, check
    whether the new reply introduces entity-like strings (numbers, proper
    nouns, dates) that appear in NEITHER the user message NOR the known
    tool results.

    Full semantic grounding analysis is deferred to Phase B.

    JUDGMENT CALL: This invariant is graded as ``"unknown"`` in Phase A
    unless a simple check can be performed.  The unit tests exercise the
    pass/fail/unknown cases with synthetic fixtures that have clear signals.
    """
    if turn.old_actions is None:
        # Cannot grade without old trace — unknown.
        return "unknown"

    new_reply = turn.new_reply or ""
    if not new_reply:
        return "unknown"

    # Build the grounding corpus.
    # Phase-B-ready: if grounding_sources is provided use it directly.
    if turn.grounding_sources is not None:
        grounding_corpus = " ".join(s.lower() for s in turn.grounding_sources)
    else:
        # Phase A heuristic: user message + tool output strings.
        grounding_corpus = turn.user_message.lower()
        for action in turn.new_actions:
            if action.raw_output:
                grounding_corpus += " " + action.raw_output.lower()

    # Heuristic: look for numbers that appear in the reply but NOT in grounding.
    # This catches the common fabrication pattern of invented counts/prices/dates.
    # More sophisticated checks (entity extraction) belong in Phase B.
    number_pattern = re.compile(r"\b\d{2,}\b")  # 2+ digit numbers
    reply_numbers = set(number_pattern.findall(new_reply))
    grounding_numbers = set(number_pattern.findall(grounding_corpus))

    # Any number in the reply that is not in the grounding corpus is suspicious.
    unsupported_numbers = reply_numbers - grounding_numbers
    if unsupported_numbers:
        return "fail"

    return "pass"


# ---------------------------------------------------------------------------
# Convenience: run all 6 invariants
# ---------------------------------------------------------------------------


@dataclass
class InvariantResults:
    """Container for all 6 invariant verdicts for one turn."""

    phantom_save: InvariantVerdict = "unknown"
    unsolicited_write: InvariantVerdict = "unknown"
    tool_loop: InvariantVerdict = "unknown"
    imprecise_following: InvariantVerdict = "unknown"
    atomicity: InvariantVerdict = "unknown"
    fabricated_state: InvariantVerdict = "unknown"

    def as_dict(self) -> dict[str, InvariantVerdict]:
        return {
            "phantom_save": self.phantom_save,
            "unsolicited_write": self.unsolicited_write,
            "tool_loop": self.tool_loop,
            "imprecise_following": self.imprecise_following,
            "atomicity": self.atomicity,
            "fabricated_state": self.fabricated_state,
        }

    def any_fail(self) -> bool:
        return any(v == "fail" for v in self.as_dict().values())

    def any_pass(self) -> bool:
        return any(v == "pass" for v in self.as_dict().values())


def check_all_invariants(
    turn: TurnRecord,
    expectations: ReplayExpectations,
    *,
    new_write_records: list | None = None,
) -> InvariantResults:
    """Run all 6 invariant checkers and return a consolidated result.

    Parameters
    ----------
    new_write_records :
        ``WriteRecord`` list from ``ReplayWriteRecorder.get_records()``.
        Required for invariants #1 and #2; others degrade gracefully if None.
    """
    return InvariantResults(
        phantom_save=check_phantom_save(
            turn, expectations, new_write_records=new_write_records
        ),
        unsolicited_write=check_unsolicited_write(
            turn, expectations, new_write_records=new_write_records
        ),
        tool_loop=check_tool_loop(turn, expectations),
        imprecise_following=check_imprecise_following(turn, expectations),
        atomicity=check_atomicity(turn, expectations),
        fabricated_state=check_fabricated_state(turn, expectations),
    )


__all__ = [
    "ActionRecord",
    "InvariantResults",
    "InvariantVerdict",
    "TurnRecord",
    "check_all_invariants",
    "check_atomicity",
    "check_fabricated_state",
    "check_imprecise_following",
    "check_phantom_save",
    "check_tool_loop",
    "check_unsolicited_write",
]
