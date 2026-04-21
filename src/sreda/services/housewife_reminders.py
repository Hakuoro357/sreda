"""Housewife reminders — domain service (no LLM, pure DB logic).

Used both by LLM-tool closures (from chat) and by the background worker
that fires due reminders. Keep this module import-cheap — no LangChain,
no LLM clients.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from dateutil.rrule import rrulestr
from sqlalchemy.orm import Session

from sreda.db.models.housewife import FamilyReminder

logger = logging.getLogger(__name__)


# Escalation policy (v1.2 reminder escalation).
#
# When a reminder fires the user sees an inline keyboard with
# "Сделал ✅" and "Отложить ⏰". If neither button is tapped the
# worker re-pings once after ESCALATION_INTERVAL_MINUTES. After
# ESCALATION_MAX_FIRES total messages the reminder finalises
# (one-shot → status=fired; recurring → next rrule occurrence), and
# escalation_count resets for the next cycle.
ESCALATION_INTERVAL_MINUTES = 2
ESCALATION_MAX_FIRES = 2  # original message + 1 re-ping
SNOOZE_DEFAULT_MINUTES = 10


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime) -> datetime:
    """Normalise any datetime to UTC.

    Rules:
      * naive → assume UTC (tenant contract: all DB values are UTC).
      * aware non-UTC → convert to UTC (``astimezone``).
      * aware UTC → no-op.

    Why ``astimezone`` matters: SQLAlchemy + SQLite w/ DateTime(timezone=True)
    strips tzinfo on store. If we hand it an aware MSK datetime
    ``13:30+03:00`` without converting, SQLite stores the local portion
    ``13:30`` — which the worker later compares against ``now(UTC)``
    and (correctly) considers in the future. Result: reminders never
    fire. Always convert FIRST so stored string is genuinely UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(slots=True)
class ReminderSummary:
    id: str
    title: str
    next_trigger_at: datetime | None
    recurrence_rule: str | None
    status: str


class HousewifeReminderService:
    """Create / list / cancel / fire reminders for a tenant.

    Writes commit after each mutation — no batch transactions — so tools
    can be invoked one-at-a-time from the chat LLM tool-loop without
    risking one bad call rolling back another.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # --- user-facing (invoked from chat tools) --------------------------

    def schedule(
        self,
        *,
        tenant_id: str,
        user_id: str | None,
        title: str,
        trigger_at: datetime,
        recurrence_rule: str | None = None,
        source_memo: str | None = None,
    ) -> FamilyReminder:
        trigger_at = _coerce_utc(trigger_at)
        # Validate rrule upfront — silently accepting a bad RRULE would
        # create a reminder that never fires.
        if recurrence_rule:
            try:
                rrulestr(recurrence_rule, dtstart=trigger_at)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"invalid recurrence_rule: {exc}") from exc

        reminder = FamilyReminder(
            id=f"rem_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            title=title.strip(),
            trigger_at=trigger_at,
            next_trigger_at=trigger_at,
            recurrence_rule=recurrence_rule,
            status="pending",
            source_memo=source_memo,
        )
        self.session.add(reminder)
        self.session.commit()
        return reminder

    def list_active(
        self, *, tenant_id: str, user_id: str | None = None
    ) -> list[FamilyReminder]:
        q = (
            self.session.query(FamilyReminder)
            .filter(
                FamilyReminder.tenant_id == tenant_id,
                FamilyReminder.status == "pending",
            )
            .order_by(FamilyReminder.next_trigger_at.asc().nullslast())
        )
        if user_id:
            q = q.filter(FamilyReminder.user_id == user_id)
        return q.all()

    def count_active(
        self, *, tenant_id: str, user_id: str | None = None
    ) -> int:
        """Cheap count for UI badges (Mini App home screen). Avoids
        materializing rows we don't need for the "3 active" subtitle."""
        q = self.session.query(FamilyReminder).filter(
            FamilyReminder.tenant_id == tenant_id,
            FamilyReminder.status == "pending",
        )
        if user_id:
            q = q.filter(FamilyReminder.user_id == user_id)
        return q.count()

    def cancel(self, *, tenant_id: str, reminder_id: str) -> bool:
        reminder = (
            self.session.query(FamilyReminder)
            .filter(
                FamilyReminder.id == reminder_id,
                FamilyReminder.tenant_id == tenant_id,
            )
            .one_or_none()
        )
        if reminder is None or reminder.status == "cancelled":
            return False
        reminder.status = "cancelled"
        reminder.next_trigger_at = None
        reminder.updated_at = _utcnow()
        self.session.commit()
        return True

    # --- worker-facing --------------------------------------------------

    def due_now(self, *, now: datetime | None = None, limit: int = 100) -> list[FamilyReminder]:
        """Cross-tenant fetch of pending reminders whose ``next_trigger_at``
        has passed. Called by the worker; NEVER expose to chat tools —
        they must stay tenant-scoped."""
        current = _coerce_utc(now or _utcnow())
        return (
            self.session.query(FamilyReminder)
            .filter(
                FamilyReminder.status == "pending",
                FamilyReminder.next_trigger_at.isnot(None),
                FamilyReminder.next_trigger_at <= current,
            )
            .order_by(FamilyReminder.next_trigger_at.asc())
            .limit(limit)
            .all()
        )

    def mark_fired(
        self, reminder: FamilyReminder, *, now: datetime | None = None
    ) -> None:
        """Called by the worker each time the reminder is sent to the
        user. Implements the escalation state machine:

          * First send (escalation_count=0 → 1): re-ping scheduled
            ``ESCALATION_INTERVAL_MINUTES`` later; status stays
            ``pending``; recurrence NOT advanced yet.
          * Subsequent sends up to ``ESCALATION_MAX_FIRES``: same
            thing, counter keeps climbing.
          * At ``ESCALATION_MAX_FIRES``: finalise this firing cycle —
            one-shot → ``status='fired'``, ``next_trigger_at=None``;
            recurring → advance to the next RRULE occurrence and reset
            ``escalation_count``.

        Caller commits (worker does it after all fires in the batch).
        """
        current = _coerce_utc(now or _utcnow())
        reminder.last_fired_at = current
        reminder.updated_at = current
        reminder.escalation_count = (reminder.escalation_count or 0) + 1

        if reminder.escalation_count < ESCALATION_MAX_FIRES:
            # Schedule the next re-ping. Counter carries forward.
            reminder.next_trigger_at = _coerce_utc(
                current + timedelta(minutes=ESCALATION_INTERVAL_MINUTES)
            )
            return

        # Cap reached — close out this firing cycle.
        reminder.escalation_count = 0
        if not reminder.recurrence_rule:
            reminder.status = "fired"
            reminder.next_trigger_at = None
            return

        try:
            rule = rrulestr(
                reminder.recurrence_rule,
                dtstart=_coerce_utc(reminder.trigger_at),
            )
            next_occ = rule.after(current, inc=False)
        except Exception:  # noqa: BLE001
            logger.exception(
                "reminder %s: failed to compute next occurrence, marking fired",
                reminder.id,
            )
            next_occ = None

        if next_occ is None:
            reminder.status = "fired"
            reminder.next_trigger_at = None
        else:
            reminder.next_trigger_at = _coerce_utc(next_occ)

    def acknowledge(
        self, reminder: FamilyReminder, *, now: datetime | None = None
    ) -> None:
        """User tapped "Сделал ✅" — close this firing cycle immediately.

        One-shot → status='fired', next_trigger_at=None.
        Recurring → advance to next RRULE occurrence without waiting
        for the re-ping timer. Records ``acknowledged_at`` for audit
        (users can look in the admin panel and see "this fire was
        acknowledged vs not"). Escalation counter resets.
        """
        current = _coerce_utc(now or _utcnow())
        reminder.acknowledged_at = current
        reminder.updated_at = current
        reminder.escalation_count = 0

        if not reminder.recurrence_rule:
            reminder.status = "fired"
            reminder.next_trigger_at = None
            return

        try:
            rule = rrulestr(
                reminder.recurrence_rule,
                dtstart=_coerce_utc(reminder.trigger_at),
            )
            next_occ = rule.after(current, inc=False)
        except Exception:  # noqa: BLE001
            logger.exception(
                "reminder %s: failed to compute next occurrence on ack",
                reminder.id,
            )
            next_occ = None

        if next_occ is None:
            reminder.status = "fired"
            reminder.next_trigger_at = None
        else:
            reminder.next_trigger_at = _coerce_utc(next_occ)

    def snooze(
        self,
        reminder: FamilyReminder,
        *,
        minutes: int = SNOOZE_DEFAULT_MINUTES,
        now: datetime | None = None,
    ) -> None:
        """User tapped "Отложить ⏰" — push next_trigger_at forward by
        ``minutes``. Escalation counter resets so the snoozed firing
        starts fresh. ``acknowledged_at`` cleared — user didn't mark
        the reminder done.
        """
        current = _coerce_utc(now or _utcnow())
        reminder.next_trigger_at = _coerce_utc(
            current + timedelta(minutes=max(1, int(minutes or 1)))
        )
        reminder.escalation_count = 0
        reminder.acknowledged_at = None
        reminder.status = "pending"
        reminder.updated_at = current

    def as_summary(self, reminder: FamilyReminder) -> ReminderSummary:
        return ReminderSummary(
            id=reminder.id,
            title=reminder.title,
            next_trigger_at=reminder.next_trigger_at,
            recurrence_rule=reminder.recurrence_rule,
            status=reminder.status,
        )
