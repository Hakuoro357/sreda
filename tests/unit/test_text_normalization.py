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
