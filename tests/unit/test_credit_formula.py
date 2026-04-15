"""Phase 4.5a: credit formula per MiMo pricing."""

from __future__ import annotations

from sreda.services.credit_formula import credits_for


def test_omni_is_base_rate():
    assert credits_for("mimo-v2-omni", 100, 50) == 150


def test_pro_below_256k_is_double():
    assert credits_for("mimo-v2-pro", 100, 50) == 300


def test_pro_at_256k_threshold_is_quadruple():
    # Exactly at threshold → higher tier
    assert credits_for("mimo-v2-pro", 256_000, 500) == (256_000 + 500) * 4


def test_tts_is_free():
    assert credits_for("mimo-v2-tts", 500, 0) == 0


def test_unknown_model_pessimistic_double():
    # Unknown model should not under-count
    assert credits_for("future-model-x", 100, 100) == 400


def test_case_insensitive():
    assert credits_for("MIMO-V2-OMNI", 100, 50) == 150


def test_zero_tokens_zero_credits():
    assert credits_for("mimo-v2-pro", 0, 0) == 0


def test_negative_tokens_clamp_to_zero():
    # Defensive: garbage in → 0
    assert credits_for("mimo-v2-omni", -5, -3) == 0
