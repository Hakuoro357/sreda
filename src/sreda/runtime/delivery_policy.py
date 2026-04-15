"""Delivery policy — pure function deciding send/defer/drop for an
outbox message (Phase 2d).

Inputs:
  * ``profile``        — dict snapshot of ``TenantUserProfile``
                         (``timezone``, ``quiet_hours``).
  * ``skill_config``   — dict snapshot of ``TenantUserSkillConfig`` for
                         the reply's feature_key, or ``None`` if absent.
  * ``feature_key``    — which skill produced this reply; ``None`` for
                         platform-core replies (help/status/etc).
  * ``is_interactive`` — was this a reply to an inbound user message?
                         (If yes we ALWAYS send — user just asked for it.)
  * ``now_utc``        — current wall-clock time (pass the same value to
                         multiple decide calls in a batch for determinism).

Decision order:
  1. Interactive → ``Send``        (responds to the user's own command;
                                    cannot be silenced even if the user
                                    muted the skill).
  2. ``priority == "mute"`` → ``Drop``.
  3. ``priority == "urgent"`` → ``Send``   (bypass quiet-hours).
  4. Inside a quiet window → ``Defer`` until window exit.
  5. Else → ``Send``.

No DB access. No I/O. Stateless. Easy to unit-test across time zones
and corner-case windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class DeliveryKind(str, Enum):
    send = "send"
    defer = "defer"
    drop = "drop"


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    kind: DeliveryKind
    defer_until_utc: datetime | None = None
    reason: str = ""


def decide_delivery(
    *,
    profile: dict[str, Any] | None,
    skill_config: dict[str, Any] | None,
    feature_key: str | None,
    is_interactive: bool,
    now_utc: datetime,
) -> DeliveryDecision:
    # 1. Interactive replies always go out — the user just typed a
    # command and is waiting for an answer. Mute/quiet-hours can silence
    # proactive notifications but must never eat a direct response.
    if is_interactive:
        return DeliveryDecision(kind=DeliveryKind.send, reason="interactive")

    # 2. Mute blocks everything proactive from this skill.
    if skill_config and skill_config.get("notification_priority") == "mute":
        return DeliveryDecision(kind=DeliveryKind.drop, reason="muted_by_user")

    # 3. Urgent bypasses quiet-hours.
    if skill_config and skill_config.get("notification_priority") == "urgent":
        return DeliveryDecision(kind=DeliveryKind.send, reason="urgent")

    # 4. Quiet-hours enforcement (profile-level, TZ-aware).
    if profile:
        tz_name = profile.get("timezone") or "UTC"
        tz = _load_tz(tz_name)
        now_local = now_utc.astimezone(tz)
        windows = profile.get("quiet_hours") or []
        active_exits = [
            _next_exit(now_local, window)
            for window in windows
            if _in_window(now_local, window)
        ]
        if active_exits:
            # Multiple overlapping windows → wait until all expire.
            latest_exit_local = max(active_exits)
            defer_until = latest_exit_local.astimezone(timezone.utc)
            return DeliveryDecision(
                kind=DeliveryKind.defer,
                defer_until_utc=defer_until,
                reason="quiet_hours",
            )

    # 5. Default.
    return DeliveryDecision(kind=DeliveryKind.send, reason="default")


def _load_tz(tz_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _in_window(now_local: datetime, window: dict[str, Any]) -> bool:
    try:
        from_hour = int(window.get("from_hour", 0))
        to_hour = int(window.get("to_hour", 0))
    except (TypeError, ValueError):
        return False
    if from_hour == to_hour:
        # Zero-length window = never active (we don't support
        # "always quiet on weekday" via this encoding).
        return False
    weekdays = window.get("weekdays") or list(range(7))
    if now_local.weekday() not in weekdays:
        return False
    h = now_local.hour
    if from_hour < to_hour:
        # Same-day window, e.g. 10..14
        return from_hour <= h < to_hour
    # Crosses midnight, e.g. 22..8
    return h >= from_hour or h < to_hour


def _next_exit(now_local: datetime, window: dict[str, Any]) -> datetime:
    """Return the local time at which this window ends for the current
    activation. Handles same-day and midnight-crossing windows uniformly:
    take the next occurrence of ``hour == to_hour``, which is either
    today or tomorrow."""
    to_hour = int(window.get("to_hour", 0))
    candidate = now_local.replace(hour=to_hour, minute=0, second=0, microsecond=0)
    if candidate <= now_local:
        candidate = candidate + timedelta(days=1)
    return candidate
