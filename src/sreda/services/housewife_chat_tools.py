"""Housewife chat tools — LangChain tools bound to a tenant/user context.

Exposed to the LLM inside ``execute_conversation_chat`` when the
resolved chat-skill is ``housewife_assistant``. Each tool returns a
short string — the LLM reads it as feedback for the next turn.

Keep tool docstrings descriptive: LangChain's LLM-tool binding uses them
as the tool's specification, so bad docstring = bad tool use.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import tool as lc_tool
from sqlalchemy.orm import Session

from sreda.services.housewife_reminders import HousewifeReminderService

logger = logging.getLogger(__name__)


def _format_reminder_for_llm(reminder: Any) -> str:
    ts = reminder.next_trigger_at
    ts_str = ts.isoformat() if ts else "—"
    rec = f" (recurring: {reminder.recurrence_rule})" if reminder.recurrence_rule else ""
    return f"[{reminder.id}] {reminder.title} → {ts_str}{rec}"


def build_housewife_tools(
    *,
    session: Session,
    tenant_id: str,
    user_id: str | None,
) -> list[Any]:
    """Return LLM tools for the housewife skill, bound to the given
    tenant/user. Called from ``execute_conversation_chat`` when the
    feature_key resolves to ``housewife_assistant``."""

    service = HousewifeReminderService(session)

    @lc_tool
    def schedule_reminder(
        title: str, trigger_iso: str, recurrence_rule: str | None = None
    ) -> str:
        """Schedule a proactive reminder for the user.

        Use when the user asks to be reminded about something in the
        future ("напомни через 2 часа", "каждый вторник в 16:00 пиши
        про кружок"). Always resolve relative phrases ("через час",
        "завтра") to an explicit ISO-8601 datetime before calling.

        ВАЖНО: все времена хранятся в **UTC**. User's local time must be
        converted. Moscow = UTC+3, so 16:00 MSK = 13:00 UTC.

        Args:
            title: Short reminder text shown to the user (под 200
                chars). Use imperative mood, e.g. "Купить молоко",
                "Сказать Пете про кружок".
            trigger_iso: ISO-8601 datetime. Either:
                • with explicit offset: "2026-04-17T19:30:00+03:00" —
                  will be converted to UTC internally, safest choice.
                • naive (no offset): "2026-04-17T16:30:00" — treated
                  as UTC. Use ONLY if you've already done the MSK→UTC
                  math yourself. For user convenience prefer the first
                  form.
            recurrence_rule: Optional RFC-5545 RRULE string for
                recurring reminders. BYHOUR / BYMINUTE MUST be in UTC
                because rrulestr doesn't understand timezones embedded
                in the rule itself — it uses the dtstart's tz. Examples:
                - User wants каждый вторник 16:00 MSK →
                  "FREQ=WEEKLY;BYDAY=TU;BYHOUR=13;BYMINUTE=0"  ← UTC!
                - User wants каждый день в 9:00 MSK →
                  "FREQ=DAILY;BYHOUR=6;BYMINUTE=0"  ← UTC!
                Common MSK→UTC shifts: subtract 3 hours (wrap mod 24
                when crossing midnight — e.g. 01:00 MSK = 22:00 UTC of
                previous day, still fine for BYHOUR=22).
                Leave None for a one-shot reminder.

        Returns short status string with the reminder id.
        """
        try:
            trigger_at = datetime.fromisoformat(trigger_iso)
        except ValueError:
            return f"error: cannot parse trigger_iso={trigger_iso!r}"

        # Always work in UTC. Naive ISO is ambiguous — treat as already-UTC
        # (the docstring says so) rather than guessing user TZ. Aware ISO
        # with any offset gets converted to UTC so downstream compares are
        # apples-to-apples. See services/housewife_reminders._coerce_utc
        # for the SQLite reason this matters.
        if trigger_at.tzinfo is None:
            trigger_at = trigger_at.replace(tzinfo=timezone.utc)
        else:
            trigger_at = trigger_at.astimezone(timezone.utc)

        try:
            reminder = service.schedule(
                tenant_id=tenant_id,
                user_id=user_id,
                title=title,
                trigger_at=trigger_at,
                recurrence_rule=recurrence_rule or None,
                source_memo=None,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("schedule_reminder failed")
            return "error: internal"

        return f"ok:scheduled:{reminder.id}:{reminder.next_trigger_at.isoformat()}"

    @lc_tool
    def list_reminders() -> str:
        """List all pending reminders for the current user.

        Call when the user asks "что у меня в напоминаниях?",
        "какие напоминания?", etc. Returns up to 20 reminders ordered
        by next trigger time, with ids so the model can reference them
        in subsequent cancel_reminder calls.
        """
        try:
            reminders = service.list_active(tenant_id=tenant_id, user_id=user_id)
        except Exception:  # noqa: BLE001
            logger.exception("list_reminders failed")
            return "error: internal"

        if not reminders:
            return "no active reminders"
        lines = [_format_reminder_for_llm(r) for r in reminders[:20]]
        return "active reminders:\n" + "\n".join(lines)

    @lc_tool
    def cancel_reminder(reminder_id: str) -> str:
        """Cancel a pending reminder by its id.

        Args:
            reminder_id: The id returned from schedule_reminder or
                list_reminders (starts with ``rem_``).

        Returns ok on success, error otherwise.
        """
        try:
            ok = service.cancel(tenant_id=tenant_id, reminder_id=reminder_id.strip())
        except Exception:  # noqa: BLE001
            logger.exception("cancel_reminder failed")
            return "error: internal"
        return "ok:cancelled" if ok else f"error: reminder {reminder_id!r} not found"

    return [schedule_reminder, list_reminders, cancel_reminder]
