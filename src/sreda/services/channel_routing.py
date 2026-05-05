"""Channel routing for proactive outbox notifications (10.6).

Заменяет hardcoded ``channel_type="telegram"`` в 4 worker'ах
(housewife_reminder, housewife_onboarding, onboarding_aha,
proactive_events) на channel-aware маршрутизацию.

**Dual delivery semantics (Boris directive 2026-05-05):**
если у юзера есть **оба** account_id (TG и MAX) — нотификация шлётся
**в оба канала** (две независимые outbox rows). Если только один —
шлётся туда. Если ни одного — empty list (caller skip + log).

``tenant.preferred_channel`` используется только для **порядка**
доставки в списке (primary first), на сам факт duplicate не влияет.

**Why dual:** юзер может зайти в TG или в MAX в любой момент —
notification должна быть видна везде где он есть. Альтернатива (один
канал) теряла бы пользователя если он сейчас в другом приложении.

**Cost:** 2× outbox writes + 2× delivery API calls на user'а с обоими
каналами. Acceptable на ранней стадии.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from sreda.db.models.core import Tenant, User


@dataclass(frozen=True, slots=True)
class OutboxRouting:
    """Single delivery target."""

    channel: str  # 'telegram' | 'max'
    chat_id: str  # recipient id в этом channel


def resolve_outbox_routings(
    session: Session,
    *,
    tenant: Tenant | None,
    user: User | None,
) -> list[OutboxRouting]:
    """Pick all deliverable channels for proactive notification.

    Returns list of OutboxRouting — по одному на каждый available
    channel. ``tenant.preferred_channel`` влияет только на порядок:
    primary первым в списке. Caller iterates и создаёт outbox row
    на каждый routing.

    Args:
        session: SQLAlchemy session — currently unused, передаём для
            будущих расширений.
        tenant: Tenant row для определения primary channel.
        user: User row для resolve account_id. None → empty list.

    Returns:
        list[OutboxRouting] — пустой если ни одного account_id нет.
    """
    if user is None:
        return []

    pref = (tenant.preferred_channel or "") if tenant is not None else ""

    # Order по preference: primary first, then the other
    if pref == "max":
        order = [
            ("max", user.max_chat_id),
            ("telegram", user.telegram_account_id),
        ]
    elif pref == "telegram":
        order = [
            ("telegram", user.telegram_account_id),
            ("max", user.max_chat_id),
        ]
    else:
        # Default order: TG-first (legacy compat — стабильнее для
        # observability, TG row всегда первая).
        order = [
            ("telegram", user.telegram_account_id),
            ("max", user.max_chat_id),
        ]

    routings: list[OutboxRouting] = []
    for channel, chat_id in order:
        if chat_id:
            routings.append(OutboxRouting(channel=channel, chat_id=str(chat_id)))
    return routings


# Backward-compat shim: возвращает первый routing или None.
# Удалить когда все 4 producer'а обновятся на ``resolve_outbox_routings``.
def resolve_outbox_routing(
    session: Session,
    *,
    tenant: Tenant | None,
    user: User | None,
) -> OutboxRouting | None:
    """[DEPRECATED] Use ``resolve_outbox_routings`` (plural) для
    dual-channel delivery. Возвращает только первый routing — теряет
    второй канал если оба доступны.
    """
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    return routings[0] if routings else None
