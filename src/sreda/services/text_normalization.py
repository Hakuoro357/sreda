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
  >>> normalize_for_dedup("ёлка") == normalize_for_dedup("елка")
  True
  >>> normalize_for_dedup("молоко.") == normalize_for_dedup("молоко")
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
  - Codex Sub-A10 R1 MAJOR #7 — Unicode NFC normalization + ``ё→е``
    folding + boundary punctuation strip. Closes common variant
    classes: ``ёлка``/``елка``, ``молоко.``/``молоко``,
    ``хлеб ``/``хлеб``.
  - We DON'T strip *internal* punctuation, so ``M&M's`` stays
    distinct from ``MM s``.
  - Empty / whitespace-only / punctuation-only input → empty string
    (caller decides what to do with it; typically "skip dedup,
    accept whatever it is").

Hash stability — Codex Sub-A10 R1 MAJOR #6:

  ``NORMALIZATION_VERSION`` is a monotonic int that callers can
  optionally fold into their hash keys when they need to guarantee
  retries land on the same key across pymorphy3 / dictionary updates.
  Bump this constant whenever the normalization output for known
  inputs changes (e.g. pymorphy3 major upgrade). The version IS
  folded into ``compute_normalized_title_hash`` HMAC payload
  (Codex Sub-A10 R2 MAJOR #6) so bumping it produces fresh hashes
  for the same input — old rows keep working, new lookups use the
  new key.
"""

from __future__ import annotations

import string
import unicodedata
from functools import lru_cache

import pymorphy3


# Codex Sub-A10 R1 MAJOR #6 — monotonic version that bumps whenever
# normalize_for_dedup output for known inputs changes. Surfaced in
# logs / debug output so cross-version retries can be detected.
NORMALIZATION_VERSION = 1

# Codex Sub-A10 R1 MAJOR #7 — boundary punctuation to strip. We keep
# internal punctuation (so "M&M's" stays "m&m's", different from
# "ms"); only leading/trailing characters get peeled.
_BOUNDARY_PUNCT = string.punctuation + " \t\r\n"


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

    See module docstring for the contract. Empty / whitespace-only /
    punctuation-only input returns an empty string.

    Pipeline (in order):
      1. Unicode NFC normalization — composes decomposed forms so
         ``Ӑ`` (U+0410 + U+0306) and ``Ӑ`` (U+04D0) collapse.
      2. Lowercase.
      3. ё → е fold (common Russian convention; planner output is
         inconsistent on this letter).
      4. Strip boundary punctuation / whitespace.
      5. Word split + per-word lemmatization (pymorphy3 singleton).
      6. Re-join with single spaces.
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFC", title)
    text = text.lower().replace("ё", "е")
    text = text.strip(_BOUNDARY_PUNCT)
    if not text:
        return ""
    words = text.split()
    # Strip per-word boundary punctuation too (e.g. "молоко," → "молоко"),
    # but keep internal punctuation intact ("M&M's").
    words = [w.strip(_BOUNDARY_PUNCT) for w in words]
    words = [w for w in words if w]
    if not words:
        return ""
    lemmas = [_lemmatize_word(w) for w in words]
    # pymorphy3 lemmas can re-introduce ``ё`` (e.g. "елка" lemma is
    # "ёлка" in some dict versions). Re-fold post-lemma so the
    # output is stable regardless of which form the dict carries.
    return " ".join(lemmas).replace("ё", "е")


__all__ = ["NORMALIZATION_VERSION", "normalize_for_dedup"]
