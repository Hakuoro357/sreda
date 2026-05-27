"""Tests for ``services/tool_schemas/families.py`` — family taxonomy +
anti-pattern headers (Sub-A-77 item #1).

Invariants the headers must hold:

1. The 12-family closed taxonomy: ``FAMILY_HEADERS`` keys ==
   ``Family`` literal values. No silent skew between the type and the
   data dict (the module already asserts this at import, but tests
   double-cover so refactors that move the assertion don't lose it).
2. Every family has at least one anti-pattern (``min_length=1`` on the
   pydantic field).
3. Anti-pattern strings are ≥10 chars and non-duplicate across the
   whole dict (caught by ``_validate_anti_pattern_strings`` at import;
   tests verify the symptoms re-emerge on tampering).
4. Frozen models — anti-pattern lists can't be mutated post-construction.
5. Token budget — total content size stays within ~1K rough estimate
   so we know the prompt prefix cost doesn't balloon unnoticed.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from sreda.services.tool_schemas.families import (
    FAMILIES,
    FAMILY_HEADERS,
    NON_FAMILY_REDIRECTS,
    TOOL_FAMILY_MANIFEST,
    Family,
    FamilyHeader,
    NonFamilyRedirect,
    _validate_anti_pattern_strings,
    assert_manifest_matches_specs,
)


# ---------------------------------------------------------------------------
# Taxonomy completeness — Family literal vs FAMILY_HEADERS keys
# ---------------------------------------------------------------------------


def test_families_tuple_length_is_twelve() -> None:
    # Closed taxonomy invariant. Adding a 13th family is a design
    # decision: bump this test along with the literal + headers.
    assert len(FAMILIES) == 12


def test_family_headers_keys_match_family_literal() -> None:
    # Same invariant the module-level assertion enforces — keep here
    # too so future refactors that strip the import-time check don't
    # silently lose the safety net.
    assert set(FAMILY_HEADERS.keys()) == set(FAMILIES)


def test_families_tuple_has_no_duplicates() -> None:
    assert len(set(FAMILIES)) == len(FAMILIES)


# ---------------------------------------------------------------------------
# Per-family content — non-empty anti-patterns, reasonable purpose length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_has_at_least_one_anti_pattern(family: Family) -> None:
    header = FAMILY_HEADERS[family]
    # Anti-patterns are tuples (frozen); we don't care about exact count
    # here, just that something explicit exists.
    assert len(header.anti_patterns) >= 1


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_purpose_is_informative(family: Family) -> None:
    # 20-char floor (also enforced by pydantic Field min_length).
    # Catches placeholder strings like «покупки» that would slip past
    # ``min_length=1`` without being useful for the planner.
    header = FAMILY_HEADERS[family]
    assert len(header.purpose) >= 20


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_anti_pattern_item_is_informative(family: Family) -> None:
    # 10-char floor enforced by ``_validate_anti_pattern_strings``.
    for ap in FAMILY_HEADERS[family].anti_patterns:
        assert len(ap.strip()) >= 10, (
            f"Anti-pattern {ap!r} in family {family!r} is too short — "
            f"must be informative enough for the planner to use."
        )


def test_anti_patterns_are_unique_across_families() -> None:
    # Catches copy-paste bugs where the same exclusion ends up in two
    # families with no useful distinction.
    seen: dict[str, Family] = {}
    for family, header in FAMILY_HEADERS.items():
        for ap in header.anti_patterns:
            key = ap.strip().lower()
            if key in seen:
                pytest.fail(
                    f"Duplicate anti-pattern {ap!r} in families "
                    f"{seen[key]!r} and {family!r}."
                )
            seen[key] = family


# ---------------------------------------------------------------------------
# Pydantic model invariants — frozen, extra=forbid, field validation
# ---------------------------------------------------------------------------


def test_family_header_is_frozen() -> None:
    h = FAMILY_HEADERS["shopping"]
    with pytest.raises(ValidationError):
        # type: ignore[misc]
        h.purpose = "anything"  # type: ignore[assignment]


def test_family_header_rejects_empty_anti_patterns() -> None:
    with pytest.raises(ValidationError):
        FamilyHeader(
            russian_name="ТЕСТ",
            purpose="достаточно длинная цель для прохождения min_length",
            anti_patterns=(),
        )


def test_family_header_rejects_too_short_purpose() -> None:
    with pytest.raises(ValidationError):
        FamilyHeader(
            russian_name="ТЕСТ",
            purpose="коротко",  # < 20 chars
            anti_patterns=("Какой-то осмысленный текст про что не использовать.",),
        )


def test_family_header_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        FamilyHeader(  # type: ignore[call-arg]
            russian_name="ТЕСТ",
            purpose="достаточно длинная цель для прохождения min_length",
            anti_patterns=("Какой-то осмысленный текст про что не использовать.",),
            unknown_field="oops",
        )


def test_validate_anti_pattern_strings_catches_short_item() -> None:
    bad = dict(FAMILY_HEADERS)
    bad["shopping"] = FamilyHeader(
        russian_name="ПОКУПКИ",
        purpose="достаточно длинная цель для прохождения min_length",
        anti_patterns=("short",),  # 5 chars after strip
    )
    with pytest.raises(ValueError, match="shorter than 10"):
        _validate_anti_pattern_strings(bad)


def test_validate_anti_pattern_strings_catches_cross_family_duplicate() -> None:
    bad = dict(FAMILY_HEADERS)
    common = "Один и тот же текст про то же самое везде."
    bad["shopping"] = FamilyHeader(
        russian_name="ПОКУПКИ",
        purpose="достаточно длинная цель для прохождения min_length",
        anti_patterns=(common,),
    )
    bad["reminders"] = FamilyHeader(
        russian_name="НАПОМИНАНИЯ",
        purpose="достаточно длинная цель для прохождения min_length",
        anti_patterns=(common,),
    )
    with pytest.raises(ValueError, match="duplicate anti-pattern"):
        _validate_anti_pattern_strings(bad)


# ---------------------------------------------------------------------------
# Token budget — soft cap on total content size
# ---------------------------------------------------------------------------


def test_total_anti_pattern_content_is_within_budget() -> None:
    # Each family expected to consume ~50-100 tokens (≈300-600 chars in
    # Russian Cyrillic at avg 5 chars/word). 12 families × 600 = 7200
    # chars HARD cap. Soft target ≈ 5000 to leave room for the family
    # body (tool names + descriptions) and the visual scaffolding
    # the renderer adds (СЕМЬЯ: ... headers, bullet indents, blank lines).
    total_chars = sum(
        len(h.russian_name) + len(h.purpose) + sum(len(ap) for ap in h.anti_patterns)
        for h in FAMILY_HEADERS.values()
    )
    assert total_chars < 7200, (
        f"Family headers total {total_chars} chars — exceeds 7200 hard "
        f"cap. Trim anti-patterns or split a family."
    )


# ---------------------------------------------------------------------------
# Specific cross-references between families — the disambiguation core
# ---------------------------------------------------------------------------


def test_shopping_anti_patterns_redirect_to_other_families() -> None:
    # The core «купить билеты vs ПОКУПКИ» / «такси vs ПОКУПКИ»
    # disambiguation must be present — Item #1's whole point is to
    # bake this into the prompt. Verify by keyword rather than exact
    # string so we can rewrite the wording without breaking the test.
    shopping_aps = " ".join(FAMILY_HEADERS["shopping"].anti_patterns).lower()
    assert "билет" in shopping_aps or "подписк" in shopping_aps
    assert "такси" in shopping_aps or "внешн" in shopping_aps


def test_reminders_anti_patterns_distinguish_tasks_and_checklists() -> None:
    # «Напоминание во времени» vs «задача без жёсткого времени» vs
    # «чек-лист с шагами» is the classic 3-way confusion.
    rem_aps = " ".join(FAMILY_HEADERS["reminders"].anti_patterns).lower()
    assert "задач" in rem_aps
    assert "чек-лист" in rem_aps or "шаг" in rem_aps


def test_tasks_anti_patterns_distinguish_reminders_and_checklists() -> None:
    tasks_aps = " ".join(FAMILY_HEADERS["tasks"].anti_patterns).lower()
    assert "напомин" in tasks_aps
    assert "чек-лист" in tasks_aps or "инструкци" in tasks_aps


def test_recipes_anti_patterns_distinguish_menu_and_shopping() -> None:
    rec_aps = " ".join(FAMILY_HEADERS["recipes"].anti_patterns).lower()
    assert "меню" in rec_aps
    assert "покупк" in rec_aps


def test_menu_anti_patterns_distinguish_recipes_and_reminders() -> None:
    menu_aps = " ".join(FAMILY_HEADERS["menu"].anti_patterns).lower()
    assert "рецепт" in menu_aps
    assert "напомин" in menu_aps or "приготов" in menu_aps


def test_household_anti_patterns_distinguish_memory_and_reminders() -> None:
    hh_aps = " ".join(FAMILY_HEADERS["household"].anti_patterns).lower()
    assert "память" in hh_aps or "переписк" in hh_aps
    assert "напомин" in hh_aps


def test_memory_anti_patterns_distinguish_household_and_tasks() -> None:
    mem_aps = " ".join(FAMILY_HEADERS["memory"].anti_patterns).lower()
    assert "семь" in mem_aps or "аллерги" in mem_aps
    assert "задач" in mem_aps or "чек-лист" in mem_aps or "рецепт" in mem_aps


def test_web_anti_patterns_redirect_to_recipes_fallback() -> None:
    # Web search is the LAST fallback for recipes — the anti-pattern
    # should redirect to the local recipes family first.
    web_aps = " ".join(FAMILY_HEADERS["web"].anti_patterns).lower()
    assert "рецепт" in web_aps


# ---------------------------------------------------------------------------
# Codex R1 MAJOR #1 — drift between Family literal, FAMILIES tuple, and
# FAMILY_HEADERS keys is detected at import + here
# ---------------------------------------------------------------------------


def test_family_literal_and_families_tuple_agree() -> None:
    # Without checking ``get_args(Family)`` an attacker (or a future
    # commit) could add to ``Family`` without adding to ``FAMILIES`` and
    # all FAMILY_HEADERS-based code would silently miss the new value.
    assert set(get_args(Family)) == set(FAMILIES)


def test_family_literal_and_family_headers_agree() -> None:
    # Same check from the literal → headers angle.
    assert set(get_args(Family)) == set(FAMILY_HEADERS.keys())


# ---------------------------------------------------------------------------
# Codex R1 MAJOR #4 — tool family manifest covers the real 55-tool universe
# ---------------------------------------------------------------------------


def test_manifest_covers_at_least_55_tools() -> None:
    # The plan-execute architecture spec lists 55 tools (47 housewife +
    # 4 memory + 3 web + 1 get_recipe_any_source). Adding one is fine,
    # silently dropping below 55 is not.
    assert len(TOOL_FAMILY_MANIFEST) >= 55


def test_manifest_every_entry_maps_to_known_family() -> None:
    # Manifest values must be valid family literal values — catches
    # typos that would silently drop a tool from the rendered registry.
    valid = set(FAMILIES)
    for tool_name, family in TOOL_FAMILY_MANIFEST.items():
        assert family in valid, (
            f"Tool {tool_name!r} assigned to unknown family {family!r}. "
            f"Add it to ``Family`` literal first or fix the typo."
        )


def test_manifest_tool_names_are_unique() -> None:
    # Mapping prevents direct duplicates by definition, but sanity-check
    # in case anyone refactors to a list-of-tuples.
    assert len(set(TOOL_FAMILY_MANIFEST.keys())) == len(TOOL_FAMILY_MANIFEST)


def test_manifest_assigns_expected_shopping_count() -> None:
    # Counts from the plan should match the manifest. If we add a tool
    # and forget to update this test, it'll surface in CI.
    shopping_tools = [t for t, f in TOOL_FAMILY_MANIFEST.items() if f == "shopping"]
    assert len(shopping_tools) == 7


def test_manifest_assigns_expected_reminders_count() -> None:
    reminders_tools = [t for t, f in TOOL_FAMILY_MANIFEST.items() if f == "reminders"]
    assert len(reminders_tools) == 4


def test_manifest_assigns_expected_tasks_count() -> None:
    tasks_tools = [t for t, f in TOOL_FAMILY_MANIFEST.items() if f == "tasks"]
    assert len(tasks_tools) == 11


def test_manifest_assigns_expected_checklists_count() -> None:
    checklists_tools = [t for t, f in TOOL_FAMILY_MANIFEST.items() if f == "checklists"]
    assert len(checklists_tools) == 8


@pytest.mark.parametrize("tool_name", [
    "add_shopping_items", "schedule_reminder", "get_recipe", "list_shopping",
    "list_reminders", "save_recipe", "plan_week_menu", "add_family_members",
    "add_task", "create_checklist", "reply_with_buttons", "save_core_fact",
    "log_unsupported_request", "weather_tool", "get_recipe_any_source",
])
def test_manifest_includes_expected_canonical_tools(tool_name: str) -> None:
    # Spot-check the tools most likely to be referenced in tests, runbooks,
    # and the planner spec. If one disappears, fail loud.
    assert tool_name in TOOL_FAMILY_MANIFEST


def test_manifest_uses_mapping_proxy() -> None:
    # MappingProxyType makes the manifest read-only at runtime.
    from types import MappingProxyType
    assert isinstance(TOOL_FAMILY_MANIFEST, MappingProxyType)

    with pytest.raises(TypeError):
        TOOL_FAMILY_MANIFEST["new_tool"] = "shopping"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Codex R1 MAJOR #7 — non-family redirect destinations vocabulary
# ---------------------------------------------------------------------------


def test_non_family_redirects_match_literal() -> None:
    # NON_FAMILY_REDIRECTS must mirror NonFamilyRedirect literal values
    # so anti-pattern strings can be tested against a single source.
    assert set(get_args(NonFamilyRedirect)) == NON_FAMILY_REDIRECTS


def test_non_family_redirects_are_uppercase_phrases() -> None:
    # All-caps so the planner LLM doesn't accidentally match them as
    # ordinary nouns. Matches the family-name style.
    for r in NON_FAMILY_REDIRECTS:
        assert r == r.upper(), (
            f"Non-family redirect {r!r} must be uppercase to mirror "
            f"family display style."
        )


def test_non_family_redirects_do_not_collide_with_family_names() -> None:
    # A redirect string that happens to equal a family russian_name
    # would confuse the planner.
    family_names = {h.russian_name for h in FAMILY_HEADERS.values()}
    for r in NON_FAMILY_REDIRECTS:
        assert r not in family_names, (
            f"Non-family redirect {r!r} collides with family name. "
            f"Pick a distinct phrase."
        )


# ---------------------------------------------------------------------------
# Codex R1 MINOR #10 — FAMILY_HEADERS is read-only at runtime
# ---------------------------------------------------------------------------


def test_family_headers_uses_mapping_proxy() -> None:
    from types import MappingProxyType
    assert isinstance(FAMILY_HEADERS, MappingProxyType)


def test_family_headers_cannot_be_mutated() -> None:
    with pytest.raises(TypeError):
        FAMILY_HEADERS["new_family"] = FAMILY_HEADERS["shopping"]  # type: ignore[index]


# ---------------------------------------------------------------------------
# Codex R1 MAJOR #6 — anti-pattern contradictions resolved (smoke checks
# that the reconciled wording is now in place)
# ---------------------------------------------------------------------------


def test_shopping_does_not_claim_birthday_gifts_belong_to_memory() -> None:
    # Before R1 fix: shopping said «подарки → ПАМЯТЬ или ЗАДАЧИ» while
    # household said «подарок родственнику → ПОКУПКИ». Reconciled: the
    # split is now by INTENT (concrete товар vs abstract идея).
    shopping_text = " ".join(FAMILY_HEADERS["shopping"].anti_patterns)
    # The reconciled rule says abstract ideas with NO product name go
    # to ПАМЯТЬ. Concrete items stay in ПОКУПКИ.
    assert "без названия товара" in shopping_text or "абстрактных" in shopping_text


def test_reminders_does_not_claim_buying_with_time_belongs_to_shopping() -> None:
    # Before R1 fix: reminders said «не забыть купить хлеб → ПОКУПКИ».
    # Reconciled: if time is present → НАПОМИНАНИЯ; if no time → ПОКУПКИ.
    reminders_text = " ".join(FAMILY_HEADERS["reminders"].anti_patterns).lower()
    assert "время" in reminders_text and "напомин" in reminders_text


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #1 — anti-pattern redirects only target known
# destinations (family russian_names or NonFamilyRedirect values)
# ---------------------------------------------------------------------------


import re as _re


# Russian inflects: «ЗАДАЧИ» (nominative) → «ЗАДАЧАМИ» (instrumental) →
# «ЗАДАЧАХ» (prepositional) all refer to the same family. The scan test
# matches all-caps phrases against destination STEMS (the longest unique
# prefix per destination root word). This is more robust than listing
# every inflected form by hand.
def _stem(word: str) -> str:
    """Return a stable 5-char Cyrillic prefix for stem matching.

    Empirically all 12 family names + 7 non-family redirects share a
    unique 5-char prefix in their first word, so 5 is enough to
    disambiguate without false matches between (e.g.) НАПОМИНАНИЯ and
    ПАМЯТЬ. Words shorter than 5 chars use the whole word.
    """
    return word[:5] if len(word) >= 5 else word


# Words that look like all-caps Russian phrases but aren't destinations —
# emphasis on user-input fragments, common qualifier prefixes, generic
# Russian conjunctions. Whitelisting these prevents false-positives.
_REDIRECT_SCAN_ALLOWLIST: frozenset[str] = frozenset({
    "TODO",                     # in «TODO-список»
    "MVP",                      # in «без MVP-tool»
    "ЕСТЬ", "НЕТ",              # logical conjunctions in time-rule
    "ЕСЛИ",                     # leading conditional
    "СУЩЕСТВУЮЩУЮ",             # qualifier (existing entity)
    "СУЩЕСТВУЮЩИМ",             # qualifier (existing entity)
    "СУЩЕСТВУЮЩЕЙ",             # qualifier (existing entity, dative)
    "НОВОЕ",                    # emphasis marker in CREATE boundary rule
    "Y", "Z", "X",              # placeholder identifiers
    "T",                        # in «задаче T-42»
})


_ALL_CAPS_CYRILLIC_RE = _re.compile(
    # Token of 3+ uppercase Cyrillic letters or capital-letter+dash sequences
    # ("СЕМЬЯ", "ПОКУПКИ", "ЧЕК-ЛИСТЫ", "ДЕНЕЖНЫЕ ОПЕРАЦИИ" — joined later).
    r"\b[А-ЯЁ][А-ЯЁ\-]{2,}(?:\s+[А-ЯЁ][А-ЯЁ\-]+)*\b"
)


def test_anti_pattern_redirects_resolve_to_known_destinations() -> None:
    """Codex R2 MAJOR #1 + R3 MINOR #3: scan every anti-pattern for
    all-caps Cyrillic tokens. For SINGLE-word tokens, the word must
    resolve to a known destination (family russian_name or
    NonFamilyRedirect) by 5-char stem match or full-word allowlist.
    For MULTI-word tokens (e.g. «ДЕНЕЖНЫЕ ОПЕРАЦИИ»), EVERY word must
    resolve — Codex R3 caught that single-token stem match would
    accept «ДЕНЕЖНЫЕ ЛЕВОЕ-СЛОВО» because the first stem is valid.
    """
    valid_stems: set[str] = set()
    for h in FAMILY_HEADERS.values():
        for w in h.russian_name.split():
            valid_stems.add(_stem(w))
    for r in NON_FAMILY_REDIRECTS:
        for w in r.split():
            valid_stems.add(_stem(w))
    valid_full_words = set(_REDIRECT_SCAN_ALLOWLIST)

    def _word_resolves(word: str) -> bool:
        """Resolution check for a SINGLE word — exactly what Codex asked.
        No phrase-level shortcut that could miss a bad second word."""
        if word in valid_full_words:
            return True
        return _stem(word) in valid_stems

    failures: list[tuple[str, str, str]] = []
    for family, header in FAMILY_HEADERS.items():
        for ap in header.anti_patterns:
            for token in _ALL_CAPS_CYRILLIC_RE.findall(ap):
                # Decompose multi-word phrases unconditionally; never
                # short-circuit on whole-phrase stem match.
                words = token.split()
                if all(_word_resolves(w) for w in words):
                    continue
                failures.append((family, token, ap))
    assert not failures, (
        "Anti-patterns reference all-caps destinations that don't "
        "resolve to known family names or NON_FAMILY_REDIRECTS "
        "(per-word stem check):\n"
        + "\n".join(
            f"  family={f!r}: token={t!r} in anti-pattern {ap!r}"
            for f, t, ap in failures
        )
    )


# Codex R3 MINOR #3 — explicit guard against «группа <NonFamilyRedirect>»
# wording (regressed in R2 utility anti-pattern).

_GROUP_NONFAMILY_RE = _re.compile(
    r"групп\w*\s+(" + "|".join(_re.escape(r) for r in NON_FAMILY_REDIRECTS) + r")",
    flags=_re.IGNORECASE,
)


def test_no_anti_pattern_calls_non_family_redirect_a_group() -> None:
    """Anti-patterns must NOT use phrases like «группа СБОРЩИК ОТВЕТА» —
    that would imply СБОРЩИК ОТВЕТА is a tool family. Codex R3 MINOR #3.
    """
    failures: list[tuple[str, str, str]] = []
    for family, header in FAMILY_HEADERS.items():
        for ap in header.anti_patterns:
            m = _GROUP_NONFAMILY_RE.search(ap)
            if m is not None:
                failures.append((family, m.group(0), ap))
    assert not failures, (
        "Anti-patterns refer to NonFamilyRedirect values as «группа»:\n"
        + "\n".join(
            f"  family={f!r}: match={m!r} in {ap!r}"
            for f, m, ap in failures
        )
    )


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #2 — exact tool-set per family in TOOL_FAMILY_MANIFEST
# (not just per-family counts — stops a miscategorised swap from passing
# while counts stay equal)
# ---------------------------------------------------------------------------


_EXPECTED_BY_FAMILY: dict[Family, frozenset[str]] = {
    "shopping": frozenset({
        "add_shopping_items", "list_shopping", "mark_shopping_bought",
        "remove_shopping_items", "update_shopping_item",
        "update_shopping_items_category", "clear_bought_shopping",
    }),
    "reminders": frozenset({
        "schedule_reminder", "list_reminders", "update_reminder",
        "cancel_reminder",
    }),
    "recipes": frozenset({
        "save_recipe", "save_recipes_batch", "search_recipes",
        "get_recipe", "delete_recipe", "get_recipe_any_source",
    }),
    "menu": frozenset({
        "plan_week_menu", "update_menu_item", "list_menu",
        "generate_shopping_from_menu", "clear_menu",
    }),
    "household": frozenset({
        "add_family_members", "list_family_members",
        "update_family_member", "remove_family_member",
    }),
    "tasks": frozenset({
        "add_task", "list_tasks", "update_task", "complete_task",
        "uncomplete_task", "cancel_task", "delete_task",
        "attach_reminder", "detach_reminder", "link_task_to_checklist",
        "unlink_task",
    }),
    "checklists": frozenset({
        "create_checklist", "add_checklist_items", "list_checklists",
        "show_checklist", "move_task_to_checklist",
        "mark_checklist_item_done", "delete_checklist_item",
        "archive_checklist",
    }),
    "onboarding": frozenset({
        "onboarding_answered", "onboarding_deferred",
        "onboarding_complete",
    }),
    "ui": frozenset({"reply_with_buttons"}),
    "memory": frozenset({"save_core_fact", "save_episode", "recall_memory"}),
    "utility": frozenset({"log_unsupported_request"}),
    "web": frozenset({"weather_tool", "web_search_tool", "fetch_url_tool"}),
}


@pytest.mark.parametrize("family,expected_tools", list(_EXPECTED_BY_FAMILY.items()))
def test_manifest_exact_tool_set_per_family(
    family: Family, expected_tools: frozenset[str]
) -> None:
    """Each family's tool set in TOOL_FAMILY_MANIFEST must EXACTLY match
    the spec. Catches a swap (e.g. ``attach_reminder`` mis-moved to
    ``reminders`` while compensating mismove keeps counts equal).
    """
    actual_tools = frozenset(
        t for t, f in TOOL_FAMILY_MANIFEST.items() if f == family
    )
    assert actual_tools == expected_tools, (
        f"Family {family!r} expected {sorted(expected_tools)} "
        f"but manifest has {sorted(actual_tools)}. "
        f"Missing: {expected_tools - actual_tools}. "
        f"Unexpected: {actual_tools - expected_tools}."
    )


def test_manifest_total_size_is_56() -> None:
    # 7+4+6+5+4+11+8+3+1+3+1+3 = 56 (55 planned + 1 from TODO-2:
    # get_recipe_any_source). Reconciles the «4 memory» comment in
    # R1 review — memory has 3, not 4 (Codex R2 spotted this).
    assert len(TOOL_FAMILY_MANIFEST) == 56


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #3 — cross-domain boundary rules present in anti-patterns
# ---------------------------------------------------------------------------


def test_reminders_anti_patterns_explain_attach_reminder_boundary() -> None:
    """When user attaches a reminder to existing task, planner should use
    tasks.attach_reminder, not reminders.schedule_reminder. Boundary rule
    must be in anti-patterns so the planner sees it."""
    text = " ".join(FAMILY_HEADERS["reminders"].anti_patterns)
    assert "attach_reminder" in text or "существующей задаче" in text.lower()


def test_tasks_anti_patterns_explain_checklist_link_boundary() -> None:
    """link_task_to_checklist (in tasks) vs move_task_to_checklist (in
    checklists) — boundary rule must surface."""
    text = " ".join(FAMILY_HEADERS["tasks"].anti_patterns)
    assert "link_task_to_checklist" in text and "move_task_to_checklist" in text
