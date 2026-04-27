"""Housewife reminders — background worker.

Polled by ``job_runner`` each tick. Finds reminders whose
``next_trigger_at`` has passed, composes outbox messages, advances the
reminder state (one-shot → fired, recurring → next occurrence).

Pattern follows ``workers/proactive_events.py::ProactiveEventWorker``:
one class per worker, ``async def process_pending(*, limit) -> int``.
The loop ordering in ``job_runner.process_pending_jobs_once`` ensures
this worker runs BEFORE ``OutboxDeliveryWorker`` so the reminders we
enqueue get delivered in the same tick.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage, User, Workspace
from sreda.db.models.housewife import FamilyReminder
from sreda.services.housewife_reminders import (
    LATE_FIRE_GRACE_MINUTES,
    HousewifeReminderService,
)

logger = logging.getLogger(__name__)

HOUSEWIFE_FEATURE_KEY = "housewife_assistant"


class HousewifeReminderWorker:
    """Fires due ``FamilyReminder`` rows as outbox messages."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.service = HousewifeReminderService(session)

    async def process_pending(
        self, *, limit: int = 50, now: datetime | None = None
    ) -> int:
        """Find reminders whose next_trigger_at is in the past, enqueue
        outbox messages, advance reminder state. Returns count fired.

        Called once per ``job_runner`` tick. Uses a single transaction:
        either all due-at-this-tick reminders fire or none do — simpler
        to reason about than per-row commits. ``now`` override exists
        for tests; production path leaves it ``None`` and gets wall-clock."""
        current = now or datetime.now(timezone.utc)
        due = self.service.due_now(now=current, limit=limit)
        if not due:
            return 0

        fired = 0
        skipped_late = 0
        for reminder in due:
            try:
                # 2026-04-23 «баг 2b»: если напоминание просрочено больше
                # чем LATE_FIRE_GRACE_MINUTES — закрываем его silently
                # без отправки в Telegram. Типичный случай: LLM создала
                # серию one-shot'ов на прошедшие часы, воркер на первой
                # же итерации увидит их всех past-due — без этого guard'а
                # юзер получает несколько одинаковых сообщений пачкой.
                # mark_fired всё равно вызываем чтобы advance'нуть state
                # (recurring → next RRULE, one-shot → status='fired').
                trigger = reminder.next_trigger_at
                if trigger is not None:
                    if trigger.tzinfo is None:
                        trigger = trigger.replace(tzinfo=timezone.utc)
                    late_min = (current - trigger).total_seconds() / 60
                    if late_min > LATE_FIRE_GRACE_MINUTES:
                        logger.info(
                            "reminder %s: past-due by %dmin > grace %dmin, "
                            "silent-finalise",
                            reminder.id, int(late_min),
                            LATE_FIRE_GRACE_MINUTES,
                        )
                        self.service.mark_fired(reminder, now=current)
                        skipped_late += 1
                        continue

                self._enqueue_outbox_for(reminder)
                self.service.mark_fired(reminder, now=current)
                fired += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reminder %s: failed to fire, will retry next tick",
                    reminder.id,
                )
                continue
        self.session.commit()
        if fired or skipped_late:
            logger.info(
                "housewife: fired=%d skipped_late=%d",
                fired, skipped_late,
            )
        return fired

    # --- internals ------------------------------------------------------

    def _enqueue_outbox_for(self, reminder: FamilyReminder) -> None:
        chat_id = self._resolve_chat_id(reminder)
        if not chat_id:
            # No Telegram chat binding — mark fired anyway so we don't
            # keep retrying. A user without telegram_account_id can't
            # receive reminders; this is a bootstrap state, not an error.
            logger.warning(
                "reminder %s: tenant %s has no Telegram chat binding, skipping delivery",
                reminder.id,
                reminder.tenant_id,
            )
            return

        workspace_id = self._resolve_workspace_id(reminder.tenant_id)
        if not workspace_id:
            logger.warning(
                "reminder %s: tenant %s has no workspace, skipping",
                reminder.id,
                reminder.tenant_id,
            )
            return

        # Escalation UI: inline keyboard lets the user ack or snooze
        # with one tap. Callback data carries the reminder id — the
        # telegram bot callback handler parses our prefix and routes
        # to HousewifeReminderService.acknowledge / .snooze.
        from sreda.services.ui_labels import BUTTON_ACK, BUTTON_SNOOZE

        text = f"🔔 {reminder.title}"
        reply_markup = {
            "inline_keyboard": [[
                {
                    "text": BUTTON_ACK,
                    "callback_data": f"rem_done:{reminder.id}",
                },
                {
                    "text": BUTTON_SNOOZE,
                    "callback_data": f"rem_snooze:{reminder.id}",
                },
            ]],
        }
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        outbox = OutboxMessage(
            id=f"out_{uuid4().hex[:24]}",
            tenant_id=reminder.tenant_id,
            workspace_id=workspace_id,
            channel_type="telegram",
            feature_key=HOUSEWIFE_FEATURE_KEY,
            status="pending",
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        # ``OutboxMessage.user_id`` may or may not exist on the
        # current schema — set only if attribute is present.
        if hasattr(OutboxMessage, "user_id"):
            outbox.user_id = reminder.user_id
        if hasattr(OutboxMessage, "is_interactive"):
            outbox.is_interactive = False
        self.session.add(outbox)
        self.session.flush()

    def _resolve_chat_id(self, reminder: FamilyReminder) -> str | None:
        """Reminder → Telegram chat_id. Prefer the binding on the
        reminder's ``user_id`` when set; fall back to any user of the
        tenant with ``telegram_account_id``."""
        if reminder.user_id:
            user = self.session.get(User, reminder.user_id)
            if user and user.telegram_account_id:
                return user.telegram_account_id

        user = (
            self.session.query(User)
            .filter(
                User.tenant_id == reminder.tenant_id,
                User.telegram_account_id.is_not(None),
            )
            .order_by(User.id.asc())
            .first()
        )
        return user.telegram_account_id if user else None

    def _resolve_workspace_id(self, tenant_id: str) -> str | None:
        ws = (
            self.session.query(Workspace)
            .filter(Workspace.tenant_id == tenant_id)
            .order_by(Workspace.id.asc())
            .first()
        )
        return ws.id if ws else None
