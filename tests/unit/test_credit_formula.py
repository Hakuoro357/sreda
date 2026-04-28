"""Phase 4.5a + 2026-04-28: credit formula per MiMo pricing.

После 2026-04-28 update тарифов Сяоми:
- MiMo-V2.5-Pro: 2x flat (раньше 4x для контекста ≥256k)
- MiMo-V2.5: 1x flat
- 20% off-peak discount в 09:00–17:00 PDT (16:00–24:00 UTC)
"""

from __future__ import annotations

from datetime import datetime, timezone

from sreda.services.credit_formula import credits_for


# ---------------------------------------------------------------------------
# Базовые ставки (без off-peak)
# ---------------------------------------------------------------------------


def test_v25_base_is_1x():
    """MiMo-V2.5: 1 token = 1 credit."""
    assert credits_for("mimo-v2.5", 100, 50) == 150


def test_v25_pro_is_2x():
    """MiMo-V2.5-Pro: 1 token = 2 credits."""
    assert credits_for("mimo-v2.5-pro", 100, 50) == 300


def test_v25_pro_no_longer_quadruples_at_256k():
    """2026-04-28 update: rate больше не зависит от context window."""
    # Был бы 4x в старой формуле; теперь 2x.
    expected = (256_000 + 500) * 2
    assert credits_for("mimo-v2.5-pro", 256_000, 500) == expected


def test_legacy_omni_is_base_rate():
    """Legacy backwards-compat: mimo-v2-omni остаётся 1x."""
    assert credits_for("mimo-v2-omni", 100, 50) == 150


def test_legacy_pro_below_256k_is_double():
    """Legacy backwards-compat: mimo-v2-pro остаётся 2x."""
    assert credits_for("mimo-v2-pro", 100, 50) == 300


def test_legacy_pro_at_256k_no_longer_quadruples():
    """2026-04-28: и легаси «mimo-v2-pro» теперь без context-window tier'а."""
    expected = (256_000 + 500) * 2
    assert credits_for("mimo-v2-pro", 256_000, 500) == expected


def test_tts_is_free():
    assert credits_for("mimo-v2-tts", 500, 0) == 0


def test_unknown_model_pessimistic_double():
    """Unknown model — 2x чтобы не недосчитать."""
    assert credits_for("future-model-x", 100, 100) == 400


def test_case_insensitive():
    assert credits_for("MIMO-V2.5", 100, 50) == 150
    assert credits_for("MiMo-V2.5-Pro", 100, 50) == 300


def test_zero_tokens_zero_credits():
    assert credits_for("mimo-v2.5-pro", 0, 0) == 0


def test_negative_tokens_clamp_to_zero():
    assert credits_for("mimo-v2.5", -5, -3) == 0


# ---------------------------------------------------------------------------
# Off-peak discount (новое — 2026-04-28)
# ---------------------------------------------------------------------------


def test_offpeak_applies_20pct_discount_v25():
    """В off-peak окне (16:00–24:00 UTC) — 20% скидка."""
    # 09:00 PDT = 16:00 UTC — начало окна
    t = datetime(2026, 4, 28, 16, 0, tzinfo=timezone.utc)
    # 100 + 50 = 150 tokens × 1 rate × 0.8 = 120
    assert credits_for("mimo-v2.5", 100, 50, now=t) == 120


def test_offpeak_applies_to_pro_too():
    """Pro в off-peak: 100 × 2 × 0.8 = 160."""
    t = datetime(2026, 4, 28, 20, 0, tzinfo=timezone.utc)  # mid off-peak
    assert credits_for("mimo-v2.5-pro", 50, 50, now=t) == 160


def test_offpeak_end_boundary_inclusive():
    """17:00 PDT = ~24:00 UTC — последний момент окна (мы используем
    23:59:59 UTC как верх)."""
    t = datetime(2026, 4, 28, 23, 59, tzinfo=timezone.utc)
    # 100 × 1 × 0.8 = 80
    assert credits_for("mimo-v2.5", 100, 0, now=t) == 80


def test_peak_no_discount_v25():
    """В рабочее время (15:00 UTC = 8:00 PDT) — без скидки."""
    t = datetime(2026, 4, 28, 15, 0, tzinfo=timezone.utc)
    assert credits_for("mimo-v2.5", 100, 50, now=t) == 150


def test_morning_no_discount_v25():
    """Утро UTC (08:00 UTC = 01:00 PDT) — глухой night-time PDT, не off-peak."""
    t = datetime(2026, 4, 28, 8, 0, tzinfo=timezone.utc)
    assert credits_for("mimo-v2.5-pro", 100, 0, now=t) == 200  # 2x, no discount


def test_no_now_means_no_discount():
    """Backwards-compat: callers без timestamp'а получают full rate."""
    assert credits_for("mimo-v2.5-pro", 100, 50, now=None) == 300


def test_naive_datetime_treated_as_utc():
    """tzinfo=None → считаем UTC. 16:00 naive → off-peak."""
    t = datetime(2026, 4, 28, 16, 0)  # no tzinfo
    assert credits_for("mimo-v2.5", 100, 50, now=t) == 120


def test_offpeak_rounds_up():
    """Off-peak умножение даёт нецелое — округляем ВВЕРХ (в пользу
    оператора, не юзера). 1 token × 1 × 0.8 = 0.8 → 1."""
    t = datetime(2026, 4, 28, 16, 0, tzinfo=timezone.utc)
    assert credits_for("mimo-v2.5", 1, 0, now=t) == 1
