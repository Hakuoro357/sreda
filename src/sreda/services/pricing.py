"""Pricing helper — единая точка получения текущих тарифов для текстов бота.

Вместо hardcode цены в строках («990 ₽/мес») все тексты зовут
функции отсюда. Меняется цена в БД (``subscription_plans``) — тексты
автоматически подхватывают новое значение.

Кэш: 60 секунд в памяти процесса. Для free-tier-exceeded text'а
(горячий путь) это даёт экономию на одном SELECT за вызов.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, int | None]] = {}


def _now() -> float:
    return time.monotonic()


def get_monthly_price_rub(
    session: Session,
    *,
    feature_key: str = "housewife_assistant",
) -> int | None:
    """Текущая месячная цена подписки на скил, в рублях.

    Ищет самый дешёвый активный план c соответствующим ``feature_key``
    в ``subscription_plans``. Возвращает None если плана нет или все
    отключены — caller должен написать запасной текст без цены.

    Кэш на 60s на (feature_key). Менять план в БД → видимо в текстах
    не мгновенно, но достаточно быстро для ручного деплоя подписки.
    """
    cached = _cache.get(feature_key)
    now = _now()
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    from sreda.db.models.billing import SubscriptionPlan

    try:
        row = (
            session.query(SubscriptionPlan)
            .filter(SubscriptionPlan.feature_key == feature_key)
            .filter(SubscriptionPlan.is_active.is_(True))
            .order_by(SubscriptionPlan.price_rub.asc())
            .first()
        )
    except Exception:  # noqa: BLE001
        # 2026-07-18 audit fix (#8): transient DB-ошибка НЕ кэшируется —
        # раньше (now, None) ложилось в кэш на 60 с, и upsell-тексты минуту
        # показывали fallback без цены из-за одного сбойного SELECT.
        logger.exception("pricing: fetch failed for feature_key=%s", feature_key)
        return None

    # 2026-07-18 audit fix (#8): цена 0 ₽ — валидное значение (бесплатный
    # план), а не «цены нет». `or None` превращал sreda_free в None.
    price = int(row.price_rub or 0) if row is not None else None
    _cache[feature_key] = (now, price)
    return price


def format_monthly_price(
    session: Session,
    *,
    feature_key: str = "housewife_assistant",
    fallback: str = "подписку",
) -> str:
    """Строковая форма цены для подстановки в пользовательский текст.

    Примеры:
      - план есть (price=990) → ``"подписку за 990 ₽/мес"``
      - плана нет → просто ``"подписку"`` (fallback)

    Использовать в шаблонах: ``f"оформи {format_monthly_price(session)}"``.
    """
    price = get_monthly_price_rub(session, feature_key=feature_key)
    if price is None:
        return fallback
    return f"подписку за {price} ₽/мес"


def invalidate_cache(feature_key: str | None = None) -> None:
    """Сбрасывает кеш. Вызывать из admin-страницы при апдейте тарифа,
    чтобы не ждать TTL."""
    if feature_key is None:
        _cache.clear()
    else:
        _cache.pop(feature_key, None)
