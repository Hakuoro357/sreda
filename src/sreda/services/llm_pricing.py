"""LLM-прайс + честная оценка стоимости вызова в USD (#150 F1).

Инварианты (план R4 + ревью Codex/субагент):
- Ключ — **(provider_key, model)** (одна строка model у разных провайдеров может
  стоить по-разному). Ключи — по РЕАЛЬНЫМ рантайм-парам (live-smoke 2026-06-16).
- **priced ⇔ заполнены `source` (URL/инвойс) И `price_checked_at`** — иначе модель
  беспрайсовая и страницы показывают «—» (а не выдуманные деньги).
- Деньги — **Decimal** (никакого float в money-path); форматирование — на UI.
- Mercury кэширует префикс → отдаём ДИАПАЗОН: `est_usd` (с допущением о кэше) и
  `upper_usd` (без кэша, верхняя граница). Без cached-цены `est == upper`.
- Историю считаем по ТЕКУЩЕМУ прайсу (страницы помечаются «оценка по текущему
  прайсу»); версионирование цен — отдельная задача (follow-up).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_MTOK = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None = None
    cache_hit_ratio: Decimal = Decimal("0")  # 0..1 (допущение, ТОЛЬКО для est)
    source: str = ""                          # URL/инвойс — ОБЯЗАТЕЛЕН для priced
    price_checked_at: date | None = None       # ОБЯЗАТЕЛЕН для priced

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.cache_hit_ratio <= Decimal("1")):
            raise ValueError(
                f"cache_hit_ratio must be 0..1, got {self.cache_hit_ratio}"
            )


@dataclass(frozen=True, slots=True)
class CostEstimate:
    est_usd: Decimal       # с допущением о кэше
    upper_usd: Decimal     # без кэша (верхняя граница; == est, если кэша нет)
    provider_key: str
    model: str


# Сидируем ТОЛЬКО подтверждённые цены. Ключ — (provider_key, model).
_PRICES: dict[tuple[str, str], ModelPrice] = {
    # Планировщик Среды. Цены из прод-биллинга Inception (см. память
    # reference_planner_on_mercury_prod, 2026-06-15): input $0.25 / cached
    # $0.025 / output $0.75 за 1M; кэш-хит ~76% (70k-префикс).
    ("inception-mercury2", "mercury-2"): ModelPrice(
        input_per_mtok=Decimal("0.25"),
        cached_input_per_mtok=Decimal("0.025"),
        output_per_mtok=Decimal("0.75"),
        cache_hit_ratio=Decimal("0.76"),
        source="Inception prod billing 2026-06-15 (reference_planner_on_mercury_prod)",
        price_checked_at=date(2026, 6, 16),
    ),
    # Рот Среды (composer/voice). Публичный прайс OpenRouter (retrieved 2026-06-16).
    # Отдельной cached-цены OpenRouter не указывает → est == upper (без кэш-допущения).
    ("openrouter-gemini-2.5-flash-lite", "google/gemini-2.5-flash-lite"): ModelPrice(
        input_per_mtok=Decimal("0.10"),
        output_per_mtok=Decimal("0.40"),
        source="https://openrouter.ai/google/gemini-2.5-flash-lite (retrieved 2026-06-16)",
        price_checked_at=date(2026, 6, 16),
    ),
    # ОТКРЫТЫЕ ВХОДНЫЕ (план R4): сидировать при подтверждённом source. До того —
    # беспрайсовые → «—» + покрытие на страницах:
    #   ("mimo-v2.5-pro"/"mimo-v2.5", ...) — инвойс Xiaomi (уточнить у владельца);
    #   легаси chat_provider'ы внешних тенантов — по мере подтверждения прайса.
}


def _priced(provider_key: str, model: str) -> ModelPrice | None:
    """ModelPrice если пара ИЗВЕСТНА И подтверждена (source+дата); иначе None."""
    p = _PRICES.get((provider_key, model))
    if p is None:
        return None
    if not p.source or p.price_checked_at is None:
        return None  # запись без подтверждённого источника = беспрайсовая
    return p


def is_priced(provider_key: str, model: str) -> bool:
    return _priced(provider_key, model) is not None


def priced_keys() -> set[tuple[str, str]]:
    """Множество подтверждённо-оценённых (provider_key, model)."""
    return {k for k in _PRICES if _priced(*k) is not None}


def cost_estimate(
    provider_key: str,
    model: str,
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None = None,
) -> CostEstimate | None:
    """USD-оценка одного вызова.

    Возвращает None, если: пара беспрайсовая; неполная форма токенов (нет сплита
    prompt/completion, только total); аномалия (отрицательные токены). Всё в Decimal.
    """
    price = _priced(provider_key, model)
    if price is None:
        return None
    # Неполная форма: нет сплита prompt/completion (любой None — напр. legacy
    # строка только с total_tokens) → честно не оценить (Codex #150 MAJOR).
    if prompt_tokens is None or completion_tokens is None:
        return None
    pt = prompt_tokens
    ct = completion_tokens
    if pt < 0 or ct < 0:  # аномалия данных — не в деньги
        return None
    if pt == 0 and ct == 0 and (total_tokens or 0) > 0:
        # сплит отсутствует (есть только total_tokens) — incomplete → None
        # (genuine 0/0-вызов без total → ниже даст priced $0, это корректно).
        return None
    inp = price.input_per_mtok
    out = price.output_per_mtok
    has_cache = price.cached_input_per_mtok is not None
    cached = price.cached_input_per_mtok if has_cache else inp
    chr_ = price.cache_hit_ratio if has_cache else Decimal("0")
    dpt, dct = Decimal(pt), Decimal(ct)
    est = dpt * (chr_ * cached + (Decimal("1") - chr_) * inp) / _MTOK + dct * out / _MTOK
    upper = dpt * inp / _MTOK + dct * out / _MTOK
    return CostEstimate(
        est_usd=est, upper_usd=upper, provider_key=provider_key, model=model,
    )
