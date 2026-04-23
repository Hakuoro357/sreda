"""Task scheduler (Расписание) — v1 MVP.

One-table module intentionally. The "Расписание" screen in the Mini
App is read-only and backed by ``tasks_items``; projects, priorities,
labels, delegation — all v1.2+. Current shape covers the 95% flow:

  * Voice/chat creates a task with date + optional time window +
    optional RRULE + optional linked reminder.
  * Mini App renders today's tasks grouped by time-of-day.
  * User completes via voice ("выполнил разминку") → status
    flips, the linked reminder cancels (for one-shots only;
    recurring reminders keep pinging on next occurrence).

The ``reminder_id`` FK is SET NULL on delete of the reminder row so
an orphaned task doesn't block a reminder's deletion and vice-versa.

EncryptedString on title / notes / delegated_to — these are the
identifying PII bits. ``scheduled_date`` / ``time_start`` / etc. stay
plaintext because they're used in index lookups and filters.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


TASK_STATUSES = ("pending", "completed", "cancelled")


class Task(Base):
    """One scheduled task in the user's planner.

    Lifecycle:
      * ``add_task`` chat tool → ``status='pending'``, optional
        ``reminder_id`` populated if the user asked for a reminder
        at creation time.
      * ``attach_reminder`` chat tool → late binding of a reminder
        to an existing task.
      * ``complete_task`` → ``status='completed'``,
        ``completed_at=now``, linked reminder cancelled if it's a
        one-shot (recurring reminder keeps advancing — the task
        repeats "морально" but each day's row is unique).
      * ``cancel_task`` → ``status='cancelled'`` + cancel reminder.
      * ``delete_task`` → hard delete row + cancel reminder.

    The worker that auto-clones recurring tasks onto tomorrow is v1.2.
    For MVP ``recurrence_rule`` is a display-only marker (🔄 icon in
    Mini App, informative in chat).
    """

    __tablename__ = "tasks_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    title: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    notes: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)

    # Null = inbox / задача без даты. Null tasks are returned by
    # ``list_tasks(date='inbox')`` only, never by today/tomorrow views.
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Both optional. time_end may be None when user just said a start
    # time. Rendered as "07:00–07:30" or "07:00" depending on shape.
    time_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    time_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    # Full RFC 5545 string. Mirrors the ``FamilyReminder.recurrence_rule``
    # shape — copied verbatim when a reminder is attached to a
    # recurring task so the ping cadence matches.
    recurrence_rule: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Optional link to a FamilyReminder. SET NULL on delete so an
    # orphan reminder doesn't drag the task down.
    reminder_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("family_reminders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Minutes before ``time_start`` that the reminder fires. Stored
    # separately from the reminder itself for display ("напомнить за
    # 15 мин") without an extra DB round-trip.
    reminder_offset_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Free-text who you delegated the task to. Encrypted (name is PII).
    # v1 doesn't actually route to the delegate — this is just a label.
    delegated_to: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_tasks_scheduled", "tenant_id", "user_id", "scheduled_date"),
        Index("ix_tasks_status", "tenant_id", "user_id", "status"),
    )
