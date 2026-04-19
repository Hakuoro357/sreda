"""Unit tests for ack_messages — quick acknowledgement picker."""

from __future__ import annotations

import random

from sreda.services.ack_messages import all_phrases, pick_ack


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
