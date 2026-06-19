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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.tasks import TASK_STATUSES, Task
from sreda.services.housewife_reminders import HousewifeReminderService

# МСК = UTC+3 круглый год; фиксированный оффсет (не требует tzdata). Для no-profile/unknown-zone
# fallback в _user_timezone — система MSK-центрична, согласованно с #168 (react naive→МСК). #166.
_MSK = timezone(timedelta(hours=3))
# sentinel: _attach_reminder_inner берёт self._bot_key, если override явно не передан.
_USE_SELF_BOT_KEY = object()

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
        bot_key: str | None = None,
    ) -> None:
        self.session = session
        self.reminders = reminder_service or HousewifeReminderService(session)
        # bot_key for new reminders created via _attach_reminder_inner.
        # Sourced from the turn's ActionEnvelope.bot_key (Phase 5).
        # Falls back to LEGACY_NULL_BOT_KEY when not provided (task-linked
        # reminders created outside a turn context, e.g. system jobs).
        self._bot_key = bot_key

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

        # #162 Фаза 0 — within-turn идемпотентность создания (эталон
        # services/housewife_shopping.py::add_items; симметрично
        # HousewifeReminderService.schedule). ctx is None → легаси байт-в-байт;
        # ctx is not None → operation_id (logical_key с scheduled_date) +
        # INSERT ON CONFLICT DO NOTHING + SELECT (стабильный id). emit_event → #163.
        from sreda.runtime.planner.tool_runtime import current_tool_runtime

        ctx = current_tool_runtime()
        # ВАЖНО (Codex MAJOR): ctx биндит И plan-execute executor, не только ReAct.
        # Идемпотентную ctx-ветку берём ТОЛЬКО для простого create (reminder_offset
        # is None). reminder-on-create (его шлёт лишь plan-execute — ReAct-схема
        # урезана) падает в legacy-путь ниже, где _attach_reminder_inner реально
        # цепляет напоминание → нет молчаливой потери напоминания у др. тенантов.
        if ctx is not None and reminder_offset_minutes is None:
            # fail-closed user_id (чеклист #162 п.8).
            if not user_id:
                raise ValueError(
                    "add ctx path: user_id обязателен (fail-closed)."
                )
            if ctx.tenant_id and ctx.tenant_id != tenant_id:
                raise ValueError(
                    "add ctx path: ctx.tenant_id="
                    f"{ctx.tenant_id!r} != tenant_id={tenant_id!r} — "
                    "ToolRuntimeContext leaked across a tenant boundary."
                )
            # reminder-on-create — ВНЕ среза (#162): урезанная ReAct-схема не
            # передаёт reminder_offset_minutes; в ctx-пути его не цепляем.
            from sreda.services.operation_id import compute_operation_id_create
            from sreda.services.text_normalization import normalize_for_dedup

            sep = "\x1f"
            logical_key = sep.join(
                [
                    normalize_for_dedup(title_clean),
                    scheduled_date.isoformat() if scheduled_date else "",
                    time_start.isoformat() if time_start else "",  # время в ключе (Codex MAJOR)
                    recurrence_rule or "",
                ]
            )
            op_id = compute_operation_id_create(
                plan_id=ctx.execution_id,
                step_id=ctx.step_id,
                action="create",
                entity_type="task",
                logical_key=logical_key,
            )
            dialect_name = self.session.bind.dialect.name  # type: ignore[union-attr]
            if dialect_name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as _insert
            else:
                from sqlalchemy.dialects.sqlite import insert as _insert  # type: ignore[no-redef]

            stmt = (
                _insert(Task)
                .values(
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
                    operation_id=op_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "user_id", "operation_id"]
                )
            )
            self.session.execute(stmt)
            self.session.commit()
            actual = (
                self.session.query(Task)
                .filter(
                    Task.tenant_id == tenant_id,
                    Task.user_id == user_id,
                    Task.operation_id == op_id,
                )
                .one_or_none()
            )
            if actual is None:
                raise RuntimeError(
                    "add ctx path: строка не найдена после INSERT для "
                    f"op_id={op_id!r} (tenant={tenant_id!r})"
                )
            return actual

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

    def add_task_with_details(
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
        details_items: list[str] | None = None,
    ) -> tuple[Task, list[str]]:
        """R-33 (2026-05-15): atomic composite create (task + checklist + items + link).

        **Fresh-session contract** (Codex R4 prescription): caller MUST own the
        transaction (open fresh session, manage commit/rollback). Этот метод НЕ
        вызывает begin/commit/rollback — только session.add + session.flush.
        Uses _no_commit variants of ChecklistService methods.

        ``details_items``:
        - None → behaves identically to ``add()`` minus the reminder attachment
          path (composite не auto-attaches reminder; use ``attach_reminder``
          в отдельном tool_call если нужно).
        - list[str] → создаёт fresh Checklist через
          ``ChecklistService.create_list_no_commit(force_new=True)`` (suffix on
          title collision), adds items, links task.checklist_id = checklist.id.

        Vendor-locked to 1-to-1 invariant via UNIQUE constraint on
        ``tasks_items.checklist_id``: каждый checklist linked максимум с
        одной task'е.

        Used by ``add_task`` LLM tool wrapper когда LLM передаёт details_items
        для composite create (e.g. «Крой: страйп белый, ледяная мята, ...»
        → task «Крой» + checklist «Крой» + 6 items + link).

        Returns ``(task, created_item_titles)`` — flushed but not committed; caller
        commits. ``created_item_titles`` (#115) are the checklist items ACTUALLY
        created (after within-batch dedup), not the raw input, so the live voice
        never over-reports duplicates. Empty when no details/checklist.
        """
        from sreda.services.checklists import ChecklistService

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
        self.session.flush()  # NO commit — caller owns TX

        # Optional composite: checklist + items + link
        created_item_titles: list[str] = []
        if details_items:
            cleaned_items = [s.strip() for s in details_items if (s or "").strip()]
            if cleaned_items:
                chk_svc = ChecklistService(self.session)
                checklist = chk_svc.create_list_no_commit(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    title=title_clean,
                    force_new=True,  # always fresh для composite path
                )
                # #115: capture the ACTUALLY-created item titles (within-batch dedup
                # already applied) — not the raw input — so we never over-report.
                created_items, _skipped = chk_svc.add_items_no_commit(
                    list_id=checklist.id,
                    items=cleaned_items,
                )
                created_item_titles = [it.title for it in created_items]
                task.checklist_id = checklist.id
                self.session.flush()

        return task, created_item_titles

    def link_to_checklist(
        self, *, tenant_id: str, user_id: str, task_id: str, checklist_id: str,
    ) -> tuple[str, str | None]:
        """R-33: explicit link existing task → existing checklist.

        Returns ``(status, error_reason)``:
        - ``("ok:linked", None)`` — newly linked
        - ``("ok:already_linked", None)`` — same pair already exists (idempotent)
        - ``("error:not_found", ...)`` — task / checklist missing or wrong owner
        - ``("error:archived", ...)`` — checklist is archived
        - ``("error:task_already_linked_to_other", existing_chk_id)``
        - ``("error:checklist_already_linked_to_other", existing_task_id)``

        Caller commits / rolls back based on result. Does NOT commit internally.
        """
        from sreda.db.models.checklists import Checklist

        task = (
            self.session.query(Task)
            .filter(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
                Task.user_id == user_id,
            )
            .one_or_none()
        )
        if task is None:
            return "error:not_found", "task_not_found"

        chk = (
            self.session.query(Checklist)
            .filter(
                Checklist.id == checklist_id,
                Checklist.tenant_id == tenant_id,
                Checklist.user_id == user_id,
            )
            .one_or_none()
        )
        if chk is None:
            return "error:not_found", "checklist_not_found"
        if chk.status != "active":
            return "error:archived", f"checklist_status={chk.status}"

        # Same pair → idempotent ok
        if task.checklist_id == checklist_id:
            return "ok:already_linked", None
        # Task linked to OTHER checklist
        if task.checklist_id is not None:
            return (
                "error:task_already_linked_to_other",
                task.checklist_id,
            )
        # Checklist already linked to OTHER task. R-33 R2 MINOR (Codex):
        # defense-in-depth — filter by tenant + user тоже, не только
        # checklist_id. Хотя service-уровне tenant_id уже ограничен
        # ownership check'ом checklist выше, явный filter защищает на
        # case multi-tenant с deleted checklist accidentally re-used.
        existing_other = (
            self.session.query(Task)
            .filter(
                Task.checklist_id == checklist_id,
                Task.tenant_id == tenant_id,
                Task.user_id == user_id,
            )
            .first()
        )
        if existing_other is not None:
            return (
                "error:checklist_already_linked_to_other",
                existing_other.id,
            )

        task.checklist_id = checklist_id
        task.updated_at = _utcnow()
        try:
            self.session.flush()
        except IntegrityError:
            # R-33 R1 MAJOR fix (Codex): concurrent link race на UNIQUE.
            # Two tasks linking к same checklist одновременно: both pass
            # in-app check (existing_other was None for both) → both UPDATE →
            # second hits UNIQUE constraint. Map to user-friendly conflict.
            # R2 MINOR (Codex): direct IntegrityError catch, no sys.exc_info.
            self.session.rollback()
            return (
                "error:checklist_already_linked_to_other",
                "concurrent_race",
            )
        return "ok:linked", None

    def unlink_from_checklist(
        self, *, tenant_id: str, user_id: str, task_id: str,
    ) -> tuple[str, str | None]:
        """R-33: unlink task from its checklist (set checklist_id = NULL).

        Returns ``(status, prev_checklist_id_or_reason)``:
        - ``("ok:unlinked", "<prev_checklist_id>")``
        - ``("ok:not_linked", None)`` — wasn't linked (idempotent)
        - ``("error:not_found", "task_not_found")``

        Caller commits. Checklist itself остаётся active независимо.
        """
        task = (
            self.session.query(Task)
            .filter(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
                Task.user_id == user_id,
            )
            .one_or_none()
        )
        if task is None:
            return "error:not_found", "task_not_found"
        if task.checklist_id is None:
            return "ok:not_linked", None
        prev = task.checklist_id
        task.checklist_id = None
        task.updated_at = _utcnow()
        self.session.flush()
        return "ok:unlinked", prev

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
        field call ``detach_reminder`` / use a dedicated clearer.
        ``recurrence_rule=""`` clears recurrence (task becomes one-shot;
        a linked reminder is recreated as one-shot — #166)."""
        task = self._get(tenant_id, user_id, task_id)
        if task is None:
            return None

        schedule_changed = False
        if title is not None:
            task.title = title.strip()[:500]
        # schedule_changed — ТОЛЬКО при РЕАЛЬНОМ изменении значения (#166: иначе передача
        # текущих даты/времени вместе с правкой текста зря пере-создаёт напоминание).
        if scheduled_date is not None and scheduled_date != task.scheduled_date:
            task.scheduled_date = scheduled_date
            schedule_changed = True
        if time_start is not None and time_start != task.time_start:
            task.time_start = time_start
            schedule_changed = True
        if time_end is not None:
            task.time_end = time_end
        if recurrence_rule is not None:
            new_rrule = recurrence_rule or None
            if new_rrule != task.recurrence_rule:
                task.recurrence_rule = new_rrule
                # #166: смена повторения тоже ресинкает связанное напоминание (иначе reminder
                # оставался бы со старым rrule; раньше синкалось лишь случайно при изменении даты).
                schedule_changed = True
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
            # сохранить bot_key СТАРОГО напоминания → новое останется на том же боте/канале
            # (#166: иначе react TaskService без bot_key сбросил бы на LEGACY_NULL).
            old_rem = self.session.get(FamilyReminder, task.reminder_id)
            old_bot_key = old_rem.bot_key if old_rem is not None else self._bot_key
            self.reminders.cancel(
                tenant_id=tenant_id, reminder_id=task.reminder_id,
            )
            task.reminder_id = None
            if task.scheduled_date and task.time_start:
                self._attach_reminder_inner(
                    task=task, offset_minutes=old_offset, bot_key=old_bot_key,
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
        # #162 п.6 — no-op guard: уже завершена → НЕ двигаем completed_at/updated_at
        # на повторе внутри хода (replay) и не гасим напоминание повторно.
        if task.status == "completed":
            return task
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
        # #162 п.6 — no-op guard: уже pending → не двигаем updated_at на повторе.
        if task.status == "pending":
            return task
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
        # #162 no-op guard (Codex MAJOR): уже отменена → не двигаем updated_at и не
        # гасим напоминание повторно на replay.
        if task.status == "cancelled":
            return task
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

    def _attach_reminder_inner(self, *, task: Task, offset_minutes: int,
                               bot_key=_USE_SELF_BOT_KEY) -> None:
        """Internal helper: create a FamilyReminder, link it, commit.
        Caller guarantees the task has scheduled_date + time_start.

        Timezone rules (2026-04-23 fix for «напоминание в 20:45 вместо 17:45»
        prod incident):
          * If the task has ``recurrence_rule``: the RRULE already encodes
            UTC hours (see _HOUSEWIFE_FOOD_PROMPT — LLM converts MSK→UTC
            before writing BYHOUR). We compute the first trigger by asking
            the RRULE for its next occurrence on or after ``scheduled_date``;
            that value is correctly UTC without needing profile TZ.
          * Otherwise (one-shot): ``time_start`` is stored as the user's
            local wall-clock time. We read their TZ from TenantUserProfile
            (default UTC) and convert combine(date, time_local) → UTC.

        In both cases we subtract ``offset_minutes`` once we have the
        correct UTC fire time.
        """
        assert task.scheduled_date is not None and task.time_start is not None

        if task.recurrence_rule:
            # RRULE is UTC by contract. Compute first occurrence anchored
            # on scheduled_date 00:00 UTC so BYHOUR/BYMINUTE control the
            # fire time (rather than time_start, which might be local).
            from dateutil.rrule import rrulestr
            anchor = datetime.combine(
                task.scheduled_date, time(0, 0), tzinfo=timezone.utc,
            )
            rule = rrulestr(task.recurrence_rule, dtstart=anchor)
            first = rule.after(anchor, inc=True)
            if first is None:
                # Rule never fires — fall back to old behaviour so we at
                # least create a reminder. Should be unreachable in prod.
                trigger_dt = _combine_local(
                    task.scheduled_date, task.time_start,
                )
            else:
                trigger_dt = first
        else:
            # One-shot: interpret time_start as user's local TZ.
            tz = self._user_timezone(task.tenant_id, task.user_id)
            naive_local = datetime.combine(task.scheduled_date, task.time_start)
            aware_local = naive_local.replace(tzinfo=tz)
            trigger_dt = aware_local.astimezone(timezone.utc)

        trigger_dt = trigger_dt - timedelta(minutes=offset_minutes)
        # Copy RRULE over so a recurring task gets a recurring reminder.
        # bot_key: по умолчанию self._bot_key (Phase 5: bot_key fixed at creation);
        # при RESCHEDULE вызывающий передаёт bot_key СТАРОГО напоминания, чтобы новое не
        # сбросилось на LEGACY_NULL (#166: react TaskService создаётся без bot_key).
        reminder = self.reminders.schedule(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            title=f"⏰ {task.title}",
            trigger_at=trigger_dt,
            recurrence_rule=task.recurrence_rule,
            source_memo=f"task:{task.id}",
            bot_key=(self._bot_key if bot_key is _USE_SELF_BOT_KEY else bot_key),
        )
        task.reminder_id = reminder.id
        task.reminder_offset_minutes = offset_minutes
        task.updated_at = _utcnow()
        self.session.commit()

    def _user_timezone(self, tenant_id: str, user_id: str):
        """Resolve the user's IANA timezone for local-wall-clock ↔ UTC
        conversion. Falls back to МСК (UTC+3) if no profile row or an unknown
        zone — система MSK-центрична (#166/#168); прежний UTC-fallback давал
        напоминания задач +3ч для profile-less юзеров."""
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        except ImportError:  # pragma: no cover
            return _MSK

        from sreda.db.models.user_profile import TenantUserProfile
        profile = (
            self.session.query(TenantUserProfile)
            .filter(
                TenantUserProfile.tenant_id == tenant_id,
                TenantUserProfile.user_id == user_id,
            )
            .one_or_none()
        )
        tz_name = profile.timezone if profile else None
        if not tz_name:
            return _MSK  # нет профиля → МСК, не UTC
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return _MSK

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

    def list_range(
        self,
        *,
        tenant_id: str,
        user_id: str,
        from_date: date,
        to_date: date,
    ) -> dict[date, list[Task]]:
        """Pending tasks across ``[from_date, to_date]`` (inclusive).

        Returns a dict keyed by **every** date in the window
        (even empty ones → empty list) so callers can render a 7-day
        skeleton without special-casing misses.

        One-shots appear on their ``scheduled_date``. Recurring tasks
        (``recurrence_rule`` non-null) are expanded via
        ``dateutil.rrule.rrulestr(...).between(...)`` so a `FREQ=DAILY`
        task surfaces on every day the RRULE yields inside the window
        — without needing an auto-clone worker.

        Recurring tasks whose one-shot scheduled_date falls inside the
        window appear only once (dedup by id + date pair is implicit
        since one-shots and RRULE expansions share the same row).
        """
        from dateutil.rrule import rrulestr

        if to_date < from_date:
            return {}

        # Initialise the skeleton: every date in the window gets an
        # empty bucket upfront. Callers can iterate keys in order via
        # `sorted(result)` or use it as a lookup.
        result: dict[date, list[Task]] = {}
        cursor = from_date
        while cursor <= to_date:
            result[cursor] = []
            cursor = cursor + timedelta(days=1)

        # 1) One-shots (and first-occurrence rows of recurring tasks)
        #    whose scheduled_date falls inside the window.
        in_window = (
            self.session.query(Task)
            .filter(
                Task.tenant_id == tenant_id,
                Task.user_id == user_id,
                Task.status == "pending",
                Task.scheduled_date.isnot(None),
                Task.scheduled_date >= from_date,
                Task.scheduled_date <= to_date,
            )
            .all()
        )
        for t in in_window:
            result[t.scheduled_date].append(t)

        # 2) Recurring tasks whose scheduled_date STARTS on or before
        #    the window and has a rule. For each, ask the RRULE which
        #    days inside the window fire, and attach the row to each
        #    of those days — EXCEPT the row's own scheduled_date which
        #    step 1 already covered.
        recurring = (
            self.session.query(Task)
            .filter(
                Task.tenant_id == tenant_id,
                Task.user_id == user_id,
                Task.status == "pending",
                Task.recurrence_rule.isnot(None),
                Task.scheduled_date.isnot(None),
                Task.scheduled_date <= to_date,
            )
            .all()
        )
        window_start_dt = datetime.combine(
            from_date, time.min, tzinfo=timezone.utc,
        )
        window_end_dt = datetime.combine(
            to_date, time.min, tzinfo=timezone.utc,
        ) + timedelta(days=1)

        for t in recurring:
            try:
                start_time = t.time_start or time(0, 0)
                dtstart = datetime.combine(
                    t.scheduled_date, start_time, tzinfo=timezone.utc,
                )
                rule = rrulestr(t.recurrence_rule, dtstart=dtstart)
                for occ in rule.between(
                    window_start_dt, window_end_dt, inc=True,
                ):
                    occ_date = occ.date()
                    if occ_date not in result:
                        continue
                    if t.scheduled_date == occ_date:
                        # Already added by step 1 — don't dup.
                        continue
                    result[occ_date].append(t)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "rrule_expansion_failed task=%s rule=%r err=%s",
                    t.id, t.recurrence_rule, exc,
                )

        # Sort each bucket by time_start (no-time last), then by
        # creation order. Stable so the same expansion yields the
        # same order across calls.
        for d in result:
            result[d].sort(
                key=lambda t: (
                    t.time_start or time(23, 59),
                    t.created_at,
                ),
            )

        return result

    def list_today(
        self, *, tenant_id: str, user_id: str, today: date,
    ) -> list[Task]:
        """Thin wrapper around :meth:`list_range` for the single-day
        case. Kept as a stable contract because ``plugin.py`` and the
        chat tool both rely on it for today's count / listing."""
        return self.list_range(
            tenant_id=tenant_id, user_id=user_id,
            from_date=today, to_date=today,
        ).get(today, [])

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
