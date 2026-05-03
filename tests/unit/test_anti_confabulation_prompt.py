"""Lock-in tests for anti-confabulation rules in _CORE_SYSTEM_PROMPT.

Регрессионный target — Boris incident 2026-05-01 8:10-8:12: bot said
"Готово!" twice про автоматическую погоду перед honest "не умею".
Stage 1.1 hotfix добавил два правила в core prompt; эти тесты гарантируют
что правила не пропадут в будущих рефакторингах.

Все три теста используют ``build_system_prompt(None)`` (без feature_key)
— потому что Boris incident случился на core/general чат-пути, не в
housewife scope. Если правило случайно мигрирует в feature-addon — этот
test сразу упадёт.
"""

from __future__ import annotations

from sreda.runtime.handlers import build_system_prompt


def _core_prompt() -> str:
    """Compose system prompt without any feature addon. Returns the
    text the LLM would see on a generic chat turn (no skill match)."""
    return build_system_prompt(None).lower()


def _all_positions(text: str, needle: str) -> list[int]:
    """All non-overlapping start positions of needle in text (lowercased).

    Empty needle returns []. Without this guard, ``str.find("", start)``
    always returns ``start`` and we'd infinite-loop until MemoryError —
    flagged in independent review 2026-05-03.
    """
    if not needle:
        return []
    text = text.lower()
    needle = needle.lower()
    positions: list[int] = []
    start = 0
    while True:
        pos = text.find(needle, start)
        if pos < 0:
            return positions
        positions.append(pos)
        start = pos + 1


def _within_proximity(
    text: str, anchor_a: str, anchor_b: str, max_chars: int = 400
) -> bool:
    """Return True iff some occurrence of anchor_a and some occurrence
    of anchor_b co-occur in the text within max_chars of each other.

    ``max_chars=400`` chosen as «one bullet/paragraph» worth of
    Russian prose in our prompts (typical bullet 100-300 chars,
    400 leaves headroom for inline examples). If future prompt edits
    add long content between two anchors of one rule and tests start
    failing despite the rule being intact — first verify the rule
    indeed remained intact, then bump to 600. Don't go above 800:
    proximity should still mean «same paragraph», otherwise the
    co-location check loses meaning.

    Crucially we check ALL occurrence pairs, not just first-find of each.
    This matters when a string (e.g. ``log_unsupported_request``)
    appears once in the tool-list block AND once in the capability-rule
    block — first-find pair would lock onto the wrong copy. By scanning
    every pair we accept the match if any legitimate co-location exists.

    Avoids the false-positive of two unrelated phrases in different
    rules satisfying ``any()``."""
    a_positions = _all_positions(text, anchor_a)
    b_positions = _all_positions(text, anchor_b)
    if not a_positions or not b_positions:
        return False
    return any(
        abs(ap - bp) <= max_chars for ap in a_positions for bp in b_positions
    )


def test_anti_done_without_tool_in_core_prompt():
    """The "не говори Готово без tool-call" rule must be in the CORE
    prompt (not just the housewife addon) — Boris's incident path was
    core-only.

    We require both anchors ("готово" AND "tool-call") to appear within
    close proximity (one paragraph). Avoids false-positive matches
    where one fragment lands in an unrelated rule.
    """
    prompt = _core_prompt()
    assert _within_proximity(prompt, '"готово"', "tool-call"), (
        "_CORE_SYSTEM_PROMPT must contain a rule that forbids saying "
        '"Готово" without a tool-call in the same turn (Boris incident '
        "2026-05-01 8:10-8:12). Both anchors '\"готово\"' and 'tool-call' "
        "must co-occur in the same paragraph."
    )


def test_anti_capability_promise_in_core_prompt():
    """When user asks for a CAPABILITY the bot doesn't have, bot must
    call log_unsupported_request, not promise "Готово". This test
    asserts the rule is present in core (not just housewife)."""
    prompt = _core_prompt()
    assert _within_proximity(
        prompt, "capability", "log_unsupported_request"
    ) or _within_proximity(
        prompt, "не обещай", "log_unsupported_request"
    ), (
        "_CORE_SYSTEM_PROMPT must instruct the LLM that on capability-gap "
        "queries (e.g. 'every morning compute X and send'), the response "
        "is log_unsupported_request, NOT a fake 'Готово'. Either "
        "'capability' + 'log_unsupported_request' OR 'не обещай' + "
        "'log_unsupported_request' must co-occur in the same paragraph."
    )


def test_done_not_unconditionally_forbidden():
    """**Negative regression guard.** Stage 1.1 must not over-fire —
    the bot is allowed to say "Готово" AFTER a real tool-call. If
    Stage 1.1 prompt accidentally bans "Готово" unconditionally, every
    legitimate reminder/checklist confirmation would break.

    Risk flagged in own plan's Risks section. Use ``_within_proximity``
    (consistent with tests #1 and #2) to assert that the conditional
    marker ("пока не было tool-call" / "без tool-call") appears NEAR
    the anti-Готово rule — not in some unrelated rule elsewhere.
    """
    prompt = _core_prompt()
    conditional_near_dones = (
        _within_proximity(prompt, '"готово"', "без tool-call")
        or _within_proximity(prompt, '"готово"', "пока в этом же turn")
        or _within_proximity(prompt, '"готово"', "пока не было реального tool-call")
        or _within_proximity(prompt, '"готово"', "без соответствующего tool-call")
    )
    assert conditional_near_dones, (
        "_CORE_SYSTEM_PROMPT must include a CONDITIONAL marker (e.g. "
        "'пока не было tool-call', 'без tool-call') co-located with the "
        'anti-Готово rule. Otherwise the bot refuses to confirm '
        "legitimate actions like schedule_reminder, breaking the "
        "reminder UX. (Negative regression guard for Stage 1.1.)"
    )


def test_within_proximity_handles_empty_anchor_safely():
    """Helper safety guard: empty anchor must not infinite-loop.

    ``str.find("", start)`` always returns ``start``, so the naive
    loop in ``_all_positions`` would never terminate. A defensive
    early-return prevents MemoryError on accidental empty input.
    Flagged in independent review 2026-05-03.
    """
    # Empty anchor returns empty positions list → proximity returns False
    assert _within_proximity("some text", "", "anything") is False
    assert _within_proximity("some text", "anything", "") is False
    assert _within_proximity("some text", "", "") is False
    # Sanity: helper still works on non-empty inputs
    assert _within_proximity("hello world", "hello", "world") is True
