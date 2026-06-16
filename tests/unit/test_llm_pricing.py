"""#150 F1: прайс LLM + честная оценка стоимости (USD).

Инварианты (план R4 + ревью): ключ (provider_key, model); priced ТОЛЬКО при
подтверждённом source+price_checked_at; беспрайсовое/неполное → None (НЕ $0);
деньги — Decimal (никакого float в money-path); Mercury — диапазон est..upper
(допущение о кэше). Ключи сидируются по РЕАЛЬНЫМ рантайм-парам (live-smoke
2026-06-16): ("inception-mercury2","mercury-2"), ("openrouter-gemini-2.5-flash-lite",
"google/gemini-2.5-flash-lite").
"""
from __future__ import annotations

from decimal import Decimal

from sreda.services.llm_pricing import (
    CostEstimate,
    cost_estimate,
    is_priced,
    priced_keys,
)

MERCURY = ("inception-mercury2", "mercury-2")


def test_mercury_priced_est_below_upper() -> None:
    # 1M prompt, 0 completion: est учитывает кэш-скидку (chr≈0.76), upper — без кэша.
    est = cost_estimate(*MERCURY, prompt_tokens=1_000_000, completion_tokens=0)
    assert isinstance(est, CostEstimate)
    assert est.est_usd < est.upper_usd        # кэш-допущение дешевле верхней границы
    assert est.provider_key == "inception-mercury2"
    assert est.model == "mercury-2"


def test_money_path_is_decimal_no_float() -> None:
    est = cost_estimate(*MERCURY, prompt_tokens=1234, completion_tokens=56)
    assert isinstance(est.est_usd, Decimal)
    assert isinstance(est.upper_usd, Decimal)


def test_unknown_pair_unpriced_returns_none() -> None:
    assert cost_estimate("nope", "whatever", prompt_tokens=100, completion_tokens=50) is None
    assert not is_priced("nope", "whatever")


def test_key_is_provider_AND_model() -> None:
    # Правильный provider, но чужая модель → НЕ priced (ключ — пара).
    assert is_priced(*MERCURY)
    assert not is_priced("inception-mercury2", "some-other-model")
    assert MERCURY in priced_keys()


def test_incomplete_token_shape_returns_none() -> None:
    # prompt/completion отсутствуют (0), есть только total → сплит невозможен → None.
    assert cost_estimate(*MERCURY, prompt_tokens=0, completion_tokens=0,
                         total_tokens=500) is None


def test_negative_tokens_returns_none() -> None:
    # Отрицательные токены — аномалия данных, не считаем в деньги.
    assert cost_estimate(*MERCURY, prompt_tokens=-5, completion_tokens=10) is None


def test_gemini_rot_priced_no_cache_est_equals_upper() -> None:
    # Gemini-рот сидирован (прайс OpenRouter) без cached_input → est == upper.
    gem = ("openrouter-gemini-2.5-flash-lite", "google/gemini-2.5-flash-lite")
    assert is_priced(*gem)
    est = cost_estimate(*gem, prompt_tokens=1000, completion_tokens=100)
    # input 0.10/1M, output 0.40/1M: 1000*0.10/1e6 + 100*0.40/1e6 = 0.00014.
    assert est.est_usd == est.upper_usd == Decimal("0.00014")
