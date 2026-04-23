"""TaskService — CRUD for the Task scheduler («Расписание») skill.

MVP scope: voice creates tasks via the chat LLM, Mini App renders
them read-only grouped by time-of-day. Projects / priorities /
labels / delegation — v1.2.

Linkage to ``FamilyReminder``:
  * If the user asks for a reminder at create time, or later via
    ``attach_reminder``, we generate a ``FamilyReminder`` through
    ``HousewifeReminderService`` with ``trigger_at = scheduled_datetime
    - offset_minutes`` and copy the task's ``recurrence_rule`` over
    so the ping cadence matches a recurring task.
  * On completion of a one-shot task we cancel the reminder
    (no ping for something already done). Recurring tasks keep their
    reminder active — tomorrow's occurrence should still ping.
  * On cancel / delete we cancel the linked reminder regardless.
  * On time-change (update) we reschedule the reminder's trigger_at.

Commit-per-method matches HousewifeReminderService: each mutation is
its own transaction so a bad LLM tool call in the middle of a turn
doesn't roll back the earlier good ones.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.tasks import TASK_STATUSES, Task
from sreda.services.housewife_reminders import HousewifeReminderService

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _combine_local(d: date, t: time) -> datetime:
    """Combine a local date + local time into a naive datetime, then
    mark it as UTC-equivalent for storage. The caller has already
    done timezone coercion at the LLM boundary — service treats the
    value as the user's intended wall-clock time.

    Kept separate for clarity: reminders expect UTC-aware datetimes.
    """
    return datetime.combine(d, t, tzinfo=timezone.utc)


class TaskService:
    """Task CRUD + reminder-link management.

    ``HousewifeReminderService`` is injected so tests can fake
    reminders (see ``test_task_reminder_link.py``). Production code
    constructs both with the same session.
    """

    def __init__(
        self,
        session: Session,
        reminder_service: HousewifeReminderService | None = None,
    ) -> None:
        self.session = session
        self.reminders = reminder_service or HousewifeReminderService(session)

    # ------------------------------------------------------------------
    # Create / update / delete
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        tenant_id: str,
        user_id: str,
        title: str,
        scheduled_date: date | None = None,
        time_start: time | None = None,
        time_end: time | None = None,
        recurrence_rule: str | None = None,
        notes: str | None = None,
        delegated_to: str | None = None,
        reminder_offset_minutes: int | None = None,
    ) -> Task:
        title_clean = (title or "").strip()
        if not title_clean:
            raise ValueError("title required")

        now = _utcnow()
        task = Task(
            id=f"task_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            title=title_clean[:500],
            notes=(notes or "").strip() or None,
            scheduled_date=scheduled_date,
            time_start=time_start,
            time_end=time_end,
            recurrence_rule=recurrence_rule or None,
            delegated_to=(delegated_to or "").strip() or None,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        self.session.add(task)
        self.session.flush()

        # Auto-attach a reminder if the user asked for one at creation.
        # Requires a date + time — otherwise we have nothing to pin the
        # trigger to. Silently skip if offset is set but we can't
        # compute a trigger (LLM should have caught this upstream).
        if reminder_offset_minutes is not None and scheduled_date and time_start:
            self._attach_reminder_inner(
                task=task,
                offset_minutes=reminder_offset_minutes,
            )
        else:
            self.session.commit()
        return task

    def update(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        title: str | None = None,
        scheduled_date: date | None = None,
        time_start: time | None = None,
        time_end: time | None = None,
        recurrence_rule: str | None = None,
        notes: str | None = None,
        delegated_to: str | None = None,
    ) -> Task | None:
        """Partial update. Pass only fields you want to change.
        ``None`` values mean "leave as-is"; to explicitly clear a
        field call ``detach_reminder`` / use a dedicated clearer."""
        task = self._get(tenant_id, user_id, task_id)
        if task is None:
            return None

        schedule_changed = False
        if title is not None:
            task.title = title.strip()[:500]
        if scheduled_date is not None:
            task.scheduled_date = scheduled_date
            schedule_changed = True
        if time_start is not None:
            task.time_start = time_start
            schedule_changed = True
        if time_end is not None:
            task.time_end = time_end
        if recurrence_rule is not None:
            task.recurrence_rule = recurrence_rule or None
        if notes is not None:
            task.notes = notes.strip() or None
        if delegated_to is not None:
            task.delegated_to = delegated_to.strip() or None
        task.updated_at = _utcnow()

        # If the schedule moved AND a reminder is linked, push the
        # reminder to match. Best-effort: cancel-and-recreate keeps the
        # logic simple at the cost of one extra row per change (old
        # reminder ends as status=cancelled, new one gets a fresh id).
        if schedule_changed and task.reminder_id and task.reminder_offset_minutes is not None:
            old_offset = task.reminder_offset_minutes
            self.reminders.cancel(
                tenant_id=tenant_id, reminder_id=task.reminder_id,
            )
            task.reminder_id = None
            if task.scheduled_date and task.time_start:
                self._attach_reminder_inner(
                    task=task, offset_minutes=old_offset,
                )
            else:
                task.reminder_offset_minutes = None
                self.session.commit()
        else:
            self.session.commit()
        return task

    def complete(
        self, *, tenant_id: str, user_id: str, task_id: str,
    ) -> Task | None:
        task = self._get(tenant_id, user_id, task_id)
        if task is None:
            return None
        task.status = "completed"
        task.completed_at = _utcnow()
        task.updated_at = _utcnow()
        # For one-shot tasks with a reminder, cancel the reminder —
        # pinging about a done task is noise. Recurring tasks with
        # recurring reminders stay active so tomorrow's occurrence
        # still fires.
        if task.reminder_id and not task.recurrence_rule:
            self.reminders.cancel(
                tenant_id=tenant_id, reminder_id=task.reminder_id,
            )
            task.reminder_id = None
            task.reminder_offset_minutes = None
        self.session.commit()
        return task

    def uncomplete(
        self, *, tenant_id: str, user_id: str, task_id: str,
    ) -> Task | None:
        """Flip a task back to pending. Doesn't restore the reminder
        — that got cancelled on complete and would need an explicit
        ``attach_reminder`` call to come back."""
        task = self._get(tenant_id, user_id, task_id)
        if task is None:
            return None
        task.status = "pending"
        task.completed_at = None
        task.updated_at = _utcnow()
        self.session.commit()
        return task

    def cancel(
        self, *, tenant_id: str, user_id: str, task_id: str,
    ) -> Task | None:
        """Soft-cancel. Row stays in DB, disappears from pending lists."""
        task = self._get(tenant_id, user_id, task_id)
        if task is None:
            return None
        task.status = "cancelled"
        task.updated_at = _utcnow()
        if task.reminder_id:
            self.reminders.cancel(
                tenant_id=tenant_id, reminder_id=task.reminder_id,
            )
            task.reminder_id = None
            task.reminder_offset_minutes = None
        self.session.commit()
        return task

    def delete(
        self, *, tenant_id: str, user_id: str, task_id: str,
    ) -> bool:
        """Hard delete. Cancels the reminder first (if any) so we
        don't leave a pending reminder orphaned."""
        task = self._get(tenant_id, user_id, task_id)
        if task is None:
            return False
        if task.reminder_id:
            self.reminders.cancel(
                tenant_id=tenant_id, reminder_id=task.reminder_id,
            )
        self.session.delete(task)
        self.session.commit()
        return True

    # ------------------------------------------------------------------
    # Reminder attachment (late-bind)
    # ------------------------------------------------------------------

    def attach_reminder(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        offset_minutes: int,
    ) -> Task | None:
        """Attach a reminder to a task that was created without one.

        Replaces any existing reminder (prior one cancelled). Requires
        ``scheduled_date + time_start`` — else raises ValueError.
        """
        task = self._get(tenant_id, user_id, task_id)
        if task is None:
            return None
        if not task.scheduled_date or not task.time_start:
            raise ValueError(
                "task has no scheduled datetime — can't compute reminder trigger"
            )

        if task.reminder_id:
            self.reminders.cancel(
                tenant_id=tenant_id, reminder_id=task.reminder_id,
            )
            task.reminder_id = None

        self._attach_reminder_inner(task=task, offset_minutes=offset_minutes)
        return task

    def detach_reminder(
        self, *, tenant_id: str, user_id: str, task_id: str,
    ) -> Task | None:
        task = self._get(tenant_id, user_id, task_id)
        if task is None:
            return None
        if task.reminder_id:
            self.reminders.cancel(
                tenant_id=tenant_id, reminder_id=task.reminder_id,
            )
        task.reminder_id = None
        task.reminder_offset_minutes = None
        task.updated_at = _utcnow()
        self.session.commit()
        return task

    def _attach_reminder_inner(self, *, task: Task, offset_minutes: int) -> None:
        """Internal helper: create a FamilyReminder, link it, commit.
        Caller guarantees the task has scheduled_date + time_start."""
        assert task.scheduled_date is not None and task.time_start is not None
        trigger_dt = _combine_local(task.scheduled_date, task.time_start)
        trigger_dt = trigger_dt - timedelta(minutes=offset_minutes)
        # Copy RRULE over so a recurring task gets a recurring reminder.
        reminder = self.reminders.schedule(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            title=f"⏰ {task.title}",
            trigger_at=trigger_dt,
            recurrence_rule=task.recurrence_rule,
            source_memo=f"task:{task.id}",
        )
        task.reminder_id = reminder.id
        task.reminder_offset_minutes = offset_minutes
        task.updated_at = _utcnow()
        self.session.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        scheduled_date: date | None = None,
        status: str | None = "pending",
        include_no_date: bool = False,
    ) -> list[Task]:
        q = self.session.query(Task).filter(
            Task.tenant_id == tenant_id,
            Task.user_id == user_id,
        )
        if status is not None:
            q = q.filter(Task.status == status)
        if scheduled_date is not None and not include_no_date:
            q = q.filter(Task.scheduled_date == scheduled_date)
        elif include_no_date and scheduled_date is None:
            q = q.filter(Task.scheduled_date.is_(None))
        elif scheduled_date is not None and include_no_date:
            q = q.filter(
                (Task.scheduled_date == scheduled_date)
                | (Task.scheduled_date.is_(None))
            )
        # Order: earliest time first, no-time last. Within the same
        # time, stable-by-created_at so two tasks at 10:00 show in the
        # order the user dictated them.
        return q.order_by(
            Task.scheduled_date.asc().nullslast(),
            Task.time_start.asc().nullslast(),
            Task.created_at.asc(),
        ).all()

    def list_today(
        self, *, tenant_id: str, user_id: str, today: date,
    ) -> list[Task]:
        return self.list(
            tenant_id=tenant_id, user_id=user_id,
            scheduled_date=today, status="pending",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get(self, tenant_id: str, user_id: str, task_id: str) -> Task | None:
        """Cross-tenant-safe single-row fetch."""
        return (
            self.session.query(Task)
            .filter(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
                Task.user_id == user_id,
            )
            .one_or_none()
        )

    def find_by_title(
        self,
        *,
        tenant_id: str,
        user_id: str,
        needle: str,
        scheduled_date: date | None = None,
        status: str = "pending",
    ) -> Task | None:
        """Case-insensitive substring match — used by voice flows where
        the LLM has no task_id and needs to find "разминка" among today's
        tasks. Returns the single best match or None. If multiple match,
        picks the earliest-scheduled pending one (most likely the next
        thing the user is working on)."""
        candidates = self.list(
            tenant_id=tenant_id,
            user_id=user_id,
            scheduled_date=scheduled_date,
            status=status,
        )
        low = (needle or "").strip().lower()
        if not low:
            return None
        hits = [t for t in candidates if low in (t.title or "").lower()]
        return hits[0] if hits else None
