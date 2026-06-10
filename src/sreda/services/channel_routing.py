"""Channel routing for proactive outbox notifications (10.6).

Заменяет hardcoded ``channel_type="telegram"`` в 4 worker'ах
(housewife_reminder, housewife_onboarding, onboarding_aha,
proactive_events) на channel-aware маршрутизацию.

**Dual delivery semantics (Boris directive 2026-05-05):**
если у юзера есть **оба** deliverable channels (TG: ``telegram_account_id``
+ MAX: ``max_chat_id``) — нотификация шлётся **в оба канала** (две
независимые outbox rows). Если только один deliverable — шлётся туда.
Если ни одного — empty list (caller skip + log).

**Deliverable predicate** (codex R1/R2): для TG нужен
``telegram_account_id``, для MAX — ``max_chat_id`` (НЕ ``max_account_id``).
``max_account_id`` — bot identity (ВКонтакте-style ID); ``max_chat_id``
— recipient в ``POST /messages?chat_id=...``. Может расходиться у
legacy data: account_id есть, chat_id нет → MAX undeliverable.

``tenant.preferred_channel`` используется только для **порядка**
доставки в списке (primary first), на сам факт duplicate не влияет.

**Why dual:** юзер может зайти в TG или в MAX в любой момент —
notification должна быть видна везде где он есть. Альтернатива (один
канал) теряла бы пользователя если он сейчас в другом приложении.

**Cost:** 2× outbox writes + 2× delivery API calls на user'а с обоими
каналами. Acceptable на ранней стадии.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from sreda.db.models.core import Tenant, User


def _telegram_bot_keys(
    telegram_bot_keys: Iterable[str] | Any | None,
) -> frozenset[str]:
    """Coerce the optional registry/keys param into a set of valid TG keys.

    Accepts either a ``TelegramBotRegistry`` (we read ``all_bots()``), a
    plain iterable of bot_key strings, or ``None`` (no info → empty set →
    bot_key stays ``None`` = current behaviour). Kept tolerant so callers
    can pass whatever they already have without a hard import dependency.
    """
    if telegram_bot_keys is None:
        return frozenset()
    # Duck-type a TelegramBotRegistry: it exposes ``all_bots()``.
    all_bots = getattr(telegram_bot_keys, "all_bots", None)
    if callable(all_bots):
        return frozenset(b.key for b in all_bots())
    return frozenset(str(k) for k in telegram_bot_keys)


@dataclass(frozen=True, slots=True)
class OutboxRouting:
    """Single delivery target."""

    channel: str  # 'telegram' | 'max'
    chat_id: str  # recipient id в этом channel
    # Phase 5 (second-tg-bot): bot that should deliver this outbox row.
    # Callers (proactive workers, reminder worker) set outbox.bot_key from
    # this field. None means caller will use its own fallback (LEGACY_NULL_BOT_KEY).
    bot_key: str | None = None


def resolve_outbox_routings(
    session: Session,
    *,
    tenant: Tenant | None,
    user: User | None,
    telegram_bot_keys: Iterable[str] | Any | None = None,
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
        telegram_bot_keys: optional ``TelegramBotRegistry`` (or iterable of
            valid Telegram bot_key strings). #109: when supplied AND the
            user's ``last_bot_key`` is one of these TELEGRAM keys, the
            telegram routing's ``bot_key`` is set to it so async producers
            deliver to the user's CURRENT bot (handles the sreda →
            sreda_home migration). When ``None`` / unknown key / not a TG
            key → ``bot_key`` stays ``None`` and producers use their
            existing fallback — identical to pre-#109 behaviour. MAX
            routings are never touched (always ``bot_key=None``).

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

    # #109: which Telegram bot should deliver this user's notifications.
    # Only honour last_bot_key if it names a REGISTERED Telegram bot — guards
    # against a MAX bot_key or a stale/typo'd value leaking into TG routing.
    tg_keys = _telegram_bot_keys(telegram_bot_keys)
    tg_bot_key: str | None = None
    if user.last_bot_key and user.last_bot_key in tg_keys:
        tg_bot_key = user.last_bot_key

    routings: list[OutboxRouting] = []
    for channel, chat_id in order:
        if chat_id:
            # MAX routing always keeps bot_key=None (unchanged behaviour).
            bot_key = tg_bot_key if channel == "telegram" else None
            routings.append(
                OutboxRouting(
                    channel=channel, chat_id=str(chat_id), bot_key=bot_key
                )
            )
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

    Codex R1 MINOR fix: emit DeprecationWarning так чтобы случайные
    callers (старый код / новые ошибки) не получали тихо single-channel
    behavior после миграции на dual-delivery.
    """
    import warnings
    warnings.warn(
        "resolve_outbox_routing (singular) is deprecated; "
        "use resolve_outbox_routings (plural) для dual-channel delivery. "
        "Single-channel routing is a violation of Boris's directive "
        "(2026-05-05): notifications должны идти в ОБА channel'а если "
        "юзер имеет оба account'а.",
        DeprecationWarning,
        stacklevel=2,
    )
    routings = resolve_outbox_routings(session, tenant=tenant, user=user)
    return routings[0] if routings else None
