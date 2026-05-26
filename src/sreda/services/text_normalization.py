"""Semantic-deduplication utility (Sub-A10, Group 3.1 of Plan-Execute Epic).

``normalize_for_dedup(title) -> str`` lemmatizes a Russian (or mixed)
string to a canonical form so morphological variants collapse to the
same dedup key. Used by tool-side dedup checks in housewife_chat_tools
to surface "уже есть" partial-duplicate responses for the planner.

Pattern:

  >>> normalize_for_dedup("молоко") == normalize_for_dedup("молока")
  True
  >>> normalize_for_dedup("куриные крылышки") == normalize_for_dedup("куриных крылышек")
  True
  >>> normalize_for_dedup("молоко") == normalize_for_dedup("обезжиренное молоко")
  False

Implementation notes:

  - pymorphy3 ``MorphAnalyzer`` is a heavyweight singleton (loads ~30MB
    of dictionaries on first init). We keep one module-level instance
    and reuse it across all calls.
  - ``parse(word)[0].normal_form`` picks the most-probable interpretation
    by frequency. For ambiguous tokens (e.g. ``стали`` — verb past-plural
    OR noun genitive-singular "of steel") pymorphy3 picks deterministically
    based on its trained statistics.
  - We lowercase + strip whitespace; we DON'T strip punctuation, so
    ``M&M's`` stays distinct from ``MM s``.
  - Empty / whitespace-only input → empty string (caller decides what
    to do with it; typically "skip dedup, accept whatever it is").
"""

from __future__ import annotations

from functools import lru_cache

import pymorphy3


_morph: pymorphy3.MorphAnalyzer | None = None


def _get_morph() -> pymorphy3.MorphAnalyzer:
    """Lazy singleton — the first call takes ~1s (dictionary load),
    subsequent calls hit the cached instance instantly. Lazy rather
    than module-import-time so test fixtures that patch the analyzer
    or monkeypatch around it don't get caught by an eager init.
    """
    global _morph
    if _morph is None:
        _morph = pymorphy3.MorphAnalyzer()
    return _morph


@lru_cache(maxsize=2048)
def _lemmatize_word(word: str) -> str:
    """Cache lemmatization per-word — repeated tokens (e.g. "молоко"
    appearing in many shopping items) hit the LRU instead of
    re-parsing. Bounded at 2k entries so the cache stays small."""
    morph = _get_morph()
    parsed = morph.parse(word)
    return parsed[0].normal_form if parsed else word


def normalize_for_dedup(title: str) -> str:
    """Return a canonical form of ``title`` for semantic dedup.

    See module docstring for the contract. Empty / whitespace-only
    input returns an empty string.
    """
    if not title:
        return ""
    text = title.strip().lower()
    if not text:
        return ""
    words = text.split()
    lemmas = [_lemmatize_word(w) for w in words]
    return " ".join(lemmas)


__all__ = ["normalize_for_dedup"]
