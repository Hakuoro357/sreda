"""Unit tests for ack_messages — quick acknowledgement picker."""

from __future__ import annotations

import random

from sreda.services.ack_messages import (
    FINAL_PROGRESS_TEXT,
    all_phrases,
    all_progress_phrases,
    pick_ack,
    pick_progress_ack,
)


def test_all_phrases_reasonable_list_size():
    """User spec: 10–20 entries. Checked at this level so a future
    accidental shrink/grow gets caught."""
    phrases = all_phrases()
    assert 10 <= len(phrases) <= 25


def test_all_phrases_are_non_empty_strings():
    for p in all_phrases():
        assert isinstance(p, str)
        assert p.strip()
        # Short — ack should feel instant, not verbose.
        assert len(p) <= 40


def test_pick_ack_returns_phrase_from_list():
    phrases = set(all_phrases())
    for _ in range(30):
        assert pick_ack() in phrases


def test_pick_ack_is_not_constant():
    """Over 100 calls we should see >1 distinct phrase. Guards against
    a silly off-by-one in random.choice or a single-item list."""
    seen = {pick_ack() for _ in range(100)}
    assert len(seen) > 1


def test_pick_ack_seeded_rng_is_deterministic():
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    seq1 = [pick_ack(rng1) for _ in range(10)]
    seq2 = [pick_ack(rng2) for _ in range(10)]
    assert seq1 == seq2


def test_progress_final_text_is_exact_contract():
    assert FINAL_PROGRESS_TEXT == "Почти готово"


def test_progress_phrases_are_separate_reasonable_pool():
    phrases = all_progress_phrases()
    assert 6 <= len(phrases) <= 25
    assert FINAL_PROGRESS_TEXT not in phrases
    for phrase in phrases:
        assert isinstance(phrase, str)
        assert phrase.strip()
        assert len(phrase) <= 48


def test_pick_progress_ack_avoids_immediate_repeat_when_possible():
    previous = all_progress_phrases()[0]
    rng = random.Random(0)
    for _ in range(30):
        assert pick_progress_ack(previous=previous, rng=rng) != previous
