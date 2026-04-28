"""Credit consumption formula (Phase 4.5).

Maps LLM token usage to MiMo credits for budget accounting.

**2026-04-28 update — новые тарифы Сяоми (Preferential Rate):**

  * MiMo-V2.5-Pro: 1 Token = 2 Credits (2x rate, flat)
  * MiMo-V2.5:     1 Token = 1 Credit  (1x rate, flat)
  * Both rates **no longer vary by context window size** (раньше для
    Pro был 4x в окне 256k–1M; теперь убрано).
  * **20% off-peak discount** в окне 09:00–17:00 PDT
    (= 16:00–00:00 UTC ≈ 19:00–03:00 MSK).

Legacy modelы (для backwards-compat — пока в коде где-то остаётся
старая нотация):

  * mimo-v2-omni → 1x  (alias на v2.5 base, 1 credit/token)
  * mimo-v2-pro  → 2x  (теперь без context-window tier'а)
  * mimo-v2-tts  → 0   (бесплатно в бете)

Total credits = (prompt_tokens + completion_tokens) * rate * off_peak_factor.
``off_peak_factor`` = 0.8 в off-peak окне, 1.0 иначе.

Платформенный estimate. Реальный billing — у MiMo, мы используем эту
цифру для quota enforcement и ``/stats``.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timezone


# Off-peak window: 09:00–17:00 PDT = 16:00–24:00 UTC (UTC-7).
# Используем UTC-7 фиксированно (даже зимой, когда формально PST = UTC-8) —
# Сяоми не уточнил DST-семантику, делаем консервативно по PDT.
_OFFPEAK_START_UTC = time(hour=16)  # 16:00 UTC = 9:00 PDT
_OFFPEAK_END_UTC = time(hour=23, minute=59, second=59)  # ~24:00 UTC = 17:00 PDT
_OFFPEAK_DISCOUNT = 0.8  # 20% off → multiply by 0.8


def _offpeak_factor(now: datetime | None) -> float:
    """1.0 в обычное время, 0.8 в off-peak окне (16:00–24:00 UTC).

    Не сезонная (PDT vs PST) — консервативно следуем PDT-метке Сяоми.
    Зимой (когда там PST) окно сдвинется на час, что нам не страшно
    — discount применяется немного шире, что в пользу юзера.
    """
    if now is None:
        return 1.0
    if now.tzinfo is None:
        # Naive datetime — считаем что это UTC.
        now = now.replace(tzinfo=timezone.utc)
    utc_time = now.astimezone(timezone.utc).time()
    if _OFFPEAK_START_UTC <= utc_time <= _OFFPEAK_END_UTC:
        return _OFFPEAK_DISCOUNT
    return 1.0


def credits_for(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    now: datetime | None = None,
) -> int:
    """Compute credits consumed for a single LLM call.

    Args:
        model: model name (e.g. "mimo-v2.5-pro", "mimo-v2.5", legacy
            "mimo-v2-pro" / "mimo-v2-omni"). Case-insensitive.
        prompt_tokens / completion_tokens: counts (negative clamped to 0).
        now: timestamp of the call. None → no off-peak discount applied
            (conservative — for tests / legacy callers without timing).

    Returns:
        Integer credits consumed (rounded UP via math.ceil after off-peak
        scaling — в пользу bot operator'а, не юзера).
    """
    total = max(0, prompt_tokens) + max(0, completion_tokens)
    if total == 0:
        return 0

    key = (model or "").strip().lower()

    # Free tier
    if key == "mimo-v2-tts":
        return 0

    # 2x rate models (Pro family — flat, no context-window tier)
    if key in ("mimo-v2.5-pro", "mimo-v2-pro"):
        rate = 2
    # 1x rate models (base / omni family)
    elif key in ("mimo-v2.5", "mimo-v2-omni"):
        rate = 1
    else:
        # Unknown model: pessimistic 2x так чтобы не недосчитать.
        # Operators увидят аномалию в /stats и добавят rate.
        rate = 2

    raw = total * rate
    discounted = raw * _offpeak_factor(now)
    # Округляем ВВЕРХ — в пользу bot operator'а (не юзера).
    # Кейс: 100 token * 2 rate * 0.8 = 160.0 → 160. Без off-peak — 200.
    return math.ceil(discounted)
