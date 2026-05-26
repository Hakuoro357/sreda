"""Tests for ``sreda.services.text_normalization`` (Sub-A10, Group 3.1).

The ``normalize_for_dedup`` function is the foundation of semantic
deduplication in user-facing tools (shopping items, reminders, tasks,
recipes, checklists). It lemmatizes a Russian string to its canonical
form so morphological variants collapse to the same key.

Closes:
  "молоко" == "молока" == "молоком"     (case inflections)
  "яблоко" == "яблоки" == "яблок"       (number)
  "купить молоко" == "куплю молоко"     (verbal forms)

Does NOT close:
  "молоко" != "обезжиренное молоко"     (additional attribute)
  "Маша" / "Машу"                       — proper names handled but
                                          may overlap (acceptable for
                                          dedup purposes)
"""

from __future__ import annotations

import pytest

from sreda.services.text_normalization import normalize_for_dedup


def test_simple_inflection_collapses():
    """Multiple case forms of the same noun normalize identically."""
    forms = ["молоко", "молока", "молоком", "молоке"]
    normalized = {normalize_for_dedup(f) for f in forms}
    assert len(normalized) == 1, (
        f"expected all forms to collapse, got: {normalized}"
    )


def test_number_collapses():
    """Singular and plural forms normalize identically."""
    forms = ["яблоко", "яблоки", "яблок", "яблоком"]
    normalized = {normalize_for_dedup(f) for f in forms}
    assert len(normalized) == 1, f"got: {normalized}"


def test_verbal_forms_collapse_with_object():
    """Different verb conjugations + same object should produce equal
    normalized strings."""
    a = normalize_for_dedup("купить молоко")
    b = normalize_for_dedup("куплю молоко")
    assert a == b, f"{a!r} vs {b!r}"


def test_distinct_items_stay_distinct():
    """``молоко`` and ``хлеб`` are different items — must not collapse."""
    a = normalize_for_dedup("молоко")
    b = normalize_for_dedup("хлеб")
    assert a != b


def test_attributes_disambiguate():
    """``молоко`` and ``обезжиренное молоко`` are intentionally NOT
    collapsed — the attribute changes semantic identity."""
    a = normalize_for_dedup("молоко")
    b = normalize_for_dedup("обезжиренное молоко")
    assert a != b


def test_case_insensitive_input():
    """Lower/Upper/Mixed case all collapse to the same key."""
    forms = ["МОЛОКО", "молоко", "Молоко", "МоЛоКо"]
    normalized = {normalize_for_dedup(f) for f in forms}
    assert len(normalized) == 1, f"got: {normalized}"


def test_strips_whitespace():
    """Leading/trailing whitespace ignored."""
    assert normalize_for_dedup("молоко") == normalize_for_dedup("  молоко  ")
    assert normalize_for_dedup("молоко") == normalize_for_dedup("молоко\n")


def test_empty_input():
    """Empty / whitespace-only input → empty string."""
    assert normalize_for_dedup("") == ""
    assert normalize_for_dedup("   ") == ""
    assert normalize_for_dedup("\t\n") == ""


def test_multi_word_phrase():
    """Multi-word phrases lemmatize word-by-word."""
    a = normalize_for_dedup("куриные крылышки")
    b = normalize_for_dedup("куриных крылышек")
    assert a == b, f"{a!r} vs {b!r}"


def test_non_russian_text_passes_through():
    """Latin / numbers / punctuation pass through unchanged (just
    lowercased). pymorphy3 returns them as-is."""
    assert normalize_for_dedup("Coca Cola") == "coca cola"
    assert normalize_for_dedup("Apple iPhone 15") == "apple iphone 15"


def test_punctuation_preserved():
    """Punctuation inside words (e.g. abbreviation, brand) is preserved
    enough that punctuated and unpunctuated variants stay distinct.
    We deliberately don't strip punctuation aggressively — `M&M's`
    is different from `MM s`."""
    a = normalize_for_dedup("м&м's")
    b = normalize_for_dedup("мс")
    assert a != b


def test_deterministic():
    """Same input → same output (no randomness from morph picker)."""
    a = normalize_for_dedup("стали")  # ambiguous: verb vs noun
    b = normalize_for_dedup("стали")
    assert a == b


def test_yo_to_e_normalization():
    """Codex R1 MAJOR #7 — ёлка / елка normalize to same key."""
    assert normalize_for_dedup("ёлка") == normalize_for_dedup("елка")
    assert normalize_for_dedup("Ёлка") == normalize_for_dedup("елка")


def test_boundary_punctuation_stripped():
    """Codex R1 MAJOR #7 — `молоко.` and `молоко` collapse."""
    assert normalize_for_dedup("молоко.") == normalize_for_dedup("молоко")
    assert normalize_for_dedup("!!!хлеб!!!") == normalize_for_dedup("хлеб")
    assert normalize_for_dedup("- молоко -") == normalize_for_dedup("молоко")


def test_internal_punctuation_preserved_after_strip():
    """Internal punctuation (`M&M's`) should still distinguish from
    plain letters, even after the boundary-punct strip."""
    assert normalize_for_dedup("M&M's") == normalize_for_dedup("m&m's")
    assert normalize_for_dedup("M&M's") != normalize_for_dedup("mms")


def test_punctuation_only_input_returns_empty():
    """All-punctuation input → empty string (boundary strip leaves nothing)."""
    assert normalize_for_dedup("...") == ""
    assert normalize_for_dedup("!!!") == ""
    assert normalize_for_dedup("---,,,") == ""


def test_normalization_version_exposed():
    """Codex R1 MAJOR #6 — version constant accessible for callers
    that need to fold it into hash keys for cross-version safety."""
    from sreda.services.text_normalization import NORMALIZATION_VERSION
    assert isinstance(NORMALIZATION_VERSION, int)
    assert NORMALIZATION_VERSION >= 1


def test_nfc_unicode_normalization():
    """Composed vs decomposed forms collapse. Russian doesn't have
    many decomposed forms in practice, but Latin diacritics in mixed
    text (e.g. brand names like ``Häagen-Dazs``) should be safe."""
    # 'é' as a single character (NFC) vs 'e' + combining accent (NFD)
    nfc = "café"  # single 'é' codepoint
    nfd = "café"  # 'e' + combining acute
    assert normalize_for_dedup(nfc) == normalize_for_dedup(nfd)


def test_golden_outputs():
    """Codex R3 MAJOR #5 — golden outputs for stability across
    pymorphy3 / dictionary version bumps. If any of these change,
    bump NORMALIZATION_VERSION (Sub-A10 design) and update this
    table — that's an intentional drift signal."""
    # Format: (input, expected_normalized_output)
    golden = [
        ("молоко", "молоко"),
        ("молока", "молоко"),
        ("молоком", "молоко"),
        ("куриные крылышки", "куриный крылышко"),
        ("куриных крылышек", "куриный крылышко"),
        ("Ёлка", "елка"),
        ("елка", "елка"),
        ("молоко.", "молоко"),
        ("  хлеб ", "хлеб"),
        ("Coca Cola", "coca cola"),
        ("", ""),
        ("...", ""),
    ]
    failures = []
    for input_text, expected in golden:
        actual = normalize_for_dedup(input_text)
        if actual != expected:
            failures.append(
                f"  {input_text!r:30} → {actual!r} (expected {expected!r})"
            )
    if failures:
        from sreda.services.text_normalization import NORMALIZATION_VERSION
        pytest.fail(
            f"normalize_for_dedup golden drift detected (NORMALIZATION_VERSION="
            f"{NORMALIZATION_VERSION}). If this is intentional (e.g. "
            f"pymorphy3 upgrade), bump NORMALIZATION_VERSION and update "
            f"the golden table. Mismatches:\n" + "\n".join(failures)
        )


def test_singleton_morph_analyzer():
    """The MorphAnalyzer should be reused across calls — first call
    can take seconds (loading dictionaries), subsequent calls must
    be fast. This is a smoke test that the function returns
    consistently."""
    import time
    # Warmup.
    normalize_for_dedup("молоко")

    start = time.perf_counter()
    for _ in range(100):
        normalize_for_dedup("молоко")
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, (
        f"100 normalize calls took {elapsed:.2f}s — singleton not "
        "reused, or analyzer leaks resources per call."
    )
