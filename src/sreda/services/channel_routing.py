"""Channel routing for proactive outbox notifications (10.6).

Заменяет hardcoded ``channel_type="telegram"`` в 4 worker'ах
(housewife_reminder, housewife_onboarding, onboarding_aha,
proactive_events) на channel-aware маршрутизацию.

**Decision logic:**

1. Если ``tenant.preferred_channel`` установлен И юзер имеет account
   в этом канале → use preferred
2. Иначе fallback в порядке availability: telegram → max
3. Если ни одного account_id нет → None (caller skip'ает delivery)

**Dual delivery (notification на оба канала)** — отдельный refactor;
сейчас single-channel routing для simplicity. Когда юзер хочет дубль,
можно добавить ``tenant.duplicate_to_all_channels = True``.

**Channel-link сценарии:**

После Boris's merge (2026-05-05) `User` row имеет оба
``telegram_account_id`` и ``max_account_id``. Без `preferred_channel`
default — TG (existing behavior). После миграции UI юзер сможет
выбрать в settings.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from sreda.db.models.core import Tenant, User


@dataclass(frozen=True, slots=True)
class OutboxRouting:
    """Decision: куда доставить notification."""

    channel: str  # 'telegram' | 'max'
    chat_id: str  # recipient id в этом channel


def resolve_outbox_routing(
    session: Session,
    *,
    tenant: Tenant | None,
    user: User | None,
) -> OutboxRouting | None:
    """Pick channel + chat_id для proactive notification.

    Args:
        session: SQLAlchemy session — currently unused, but передаём для
            future расширений (e.g. lookup tenant.preferred_channel
            если ``tenant`` is None).
        tenant: Tenant row для check'а ``preferred_channel``. Если None —
            используем default fallback порядок.
        user: User row для resolve account_id. Если None — None результат.

    Returns:
        OutboxRouting если найден доставимый канал; None если не нашли
        ни одного account_id (caller должен skip notification + log).
    """
    if user is None:
        return None

    pref = (tenant.preferred_channel or "") if tenant is not None else ""

    # Build candidates в priority порядке
    if pref == "max":
        candidates = [
            ("max", user.max_chat_id),
            ("telegram", user.telegram_account_id),
        ]
    elif pref == "telegram":
        candidates = [
            ("telegram", user.telegram_account_id),
            ("max", user.max_chat_id),
        ]
    else:
        # Default: TG-first (legacy behavior — все existing
        # users были TG-only до 2026-05-04)
        candidates = [
            ("telegram", user.telegram_account_id),
            ("max", user.max_chat_id),
        ]

    for channel, chat_id in candidates:
        if chat_id:
            return OutboxRouting(channel=channel, chat_id=str(chat_id))
    return None
