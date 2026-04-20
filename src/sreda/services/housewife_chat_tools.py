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

from sreda.services.housewife_onboarding import (
    TOPIC_DESCRIPTIONS,
    HousewifeOnboardingService,
)
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.housewife_shopping import HousewifeShoppingService

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

    # ----------------------------------------------------------------
    # Onboarding tools. Only meaningful during the first-contact flow;
    # the LLM is told (via the [ОНБОРДИНГ] prompt block) when to use
    # them. Outside onboarding the tools are still callable but the
    # underlying service returns a neutral state.
    # ----------------------------------------------------------------
    onboarding_service = HousewifeOnboardingService(session)

    @lc_tool
    def onboarding_answered(topic: str, summary: str) -> str:
        """Mark an onboarding topic as answered and advance to the next one.

        Call this when the user has given you a meaningful answer to the
        current onboarding topic (see [ОНБОРДИНГ] in the system prompt
        for which topic is current). The ``summary`` is what will be
        stored — a 1–2 sentence paraphrase in the user's own words.

        Args:
            topic: One of: addressing, self_intro, family, diet, routine,
                pain_point. Match the [ОНБОРДИНГ] block's current_topic.
            summary: The answer in 1–2 sentences, preserving user wording.
                For the ``addressing`` topic — just the name.

        Returns short status string.
        """
        topic_norm = (topic or "").strip().lower()
        if topic_norm not in TOPIC_DESCRIPTIONS:
            return f"error: unknown topic {topic!r}"
        if not user_id:
            return "error: no user_id context"
        text = (summary or "").strip()
        if not text:
            return "error: empty summary"
        try:
            state = onboarding_service.mark_answered(
                tenant_id=tenant_id,
                user_id=user_id,
                topic=topic_norm,
                summary=text,
            )
        except Exception:  # noqa: BLE001
            logger.exception("onboarding_answered failed")
            return "error: internal"
        next_topic = state.get("current_topic") or "none"
        return f"ok:answered:{topic_norm}:next={next_topic}:status={state.get('status')}"

    @lc_tool
    def onboarding_deferred(topic: str, reason: str) -> str:
        """Mark an onboarding topic as deferred and advance to the next one.

        Call this when the user explicitly wants to skip the current topic
        ("потом", "не сейчас", "пропусти"). First skip keeps the topic in
        a retry queue; second skip on the same topic makes the skip
        permanent.

        Args:
            topic: One of: addressing, self_intro, family, diet, routine,
                pain_point.
            reason: Short explanation of why skipping — for audit.

        Returns short status string.
        """
        topic_norm = (topic or "").strip().lower()
        if topic_norm not in TOPIC_DESCRIPTIONS:
            return f"error: unknown topic {topic!r}"
        if not user_id:
            return "error: no user_id context"
        try:
            state = onboarding_service.mark_deferred(
                tenant_id=tenant_id,
                user_id=user_id,
                topic=topic_norm,
            )
        except Exception:  # noqa: BLE001
            logger.exception("onboarding_deferred failed")
            return "error: internal"
        next_topic = state.get("current_topic") or "none"
        topic_state = (
            state.get("topics", {}).get(topic_norm, {}).get("state") or "?"
        )
        return (
            f"ok:deferred:{topic_norm}:topic_state={topic_state}:"
            f"next={next_topic}:status={state.get('status')}"
        )

    @lc_tool
    def onboarding_complete() -> str:
        """Explicitly mark onboarding complete.

        Normally the flow auto-completes when all topics are closed
        (answered or permanently skipped), so you don't have to call
        this. Use only if user clearly says "всё, хватит, мне надоело"
        before natural completion.

        Returns short status string.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            state = onboarding_service.mark_complete(
                tenant_id=tenant_id, user_id=user_id
            )
        except Exception:  # noqa: BLE001
            logger.exception("onboarding_complete failed")
            return "error: internal"
        return f"ok:complete:status={state.get('status')}"

    # ----------------------------------------------------------------
    # Shopping tools (v1.1). Single global per-user list; the LLM
    # classifies categories on add. UI groups visually; chat can add
    # / remove / check off items. See services/housewife_shopping.py
    # for state lifecycle.
    # ----------------------------------------------------------------
    shopping_service = HousewifeShoppingService(session)

    @lc_tool
    def add_shopping_items(items: list[dict[str, Any]]) -> str:
        """Add items to the user's shopping list.

        Batch-add — one call for everything the user mentioned, not one
        call per item. Each item is a dict; only ``title`` is required.

        Args:
            items: List of {title, quantity_text?, category?} dicts.
                • title — item name ("молоко", "батон нарезной"). Free form.
                • quantity_text — free-form quantity label ("1 л", "2 шт",
                  "500 г"). Optional. No math done on it.
                • category — ONE of: молочные, мясо_рыба, овощи_фрукты,
                  хлеб, бакалея, напитки, готовое, замороженное,
                  бытовая_химия, другое.
                  Best-effort classify yourself — молоко→молочные,
                  хлеб→хлеб, etc. Unknown → "другое" (tool auto-maps).

        Example:
            add_shopping_items([
                {"title": "молоко", "quantity_text": "1 л", "category": "молочные"},
                {"title": "хлеб", "category": "хлеб"},
                {"title": "помидоры", "quantity_text": "1 кг", "category": "овощи_фрукты"},
            ])

        Returns a short status string with how many rows were inserted
        and the ids (needed if the user immediately changes their mind
        and you need to remove_shopping_items the last add).
        """
        if not user_id:
            return "error: no user_id context"
        if not items:
            return "error: empty items list"
        try:
            rows = shopping_service.add_items(
                tenant_id=tenant_id, user_id=user_id, items=items
            )
        except Exception:  # noqa: BLE001
            logger.exception("add_shopping_items failed")
            return "error: internal"
        if not rows:
            return "ok:added:0"
        ids_csv = ",".join(r.id for r in rows)
        return f"ok:added:{len(rows)}:ids=[{ids_csv}]"

    @lc_tool
    def mark_shopping_bought(item_ids: list[str]) -> str:
        """Mark shopping list items as bought (checked off).

        Call when the user says they've bought something. Items move to
        ``bought`` status and disappear from the normal view. Batch —
        one call for all items the user mentioned.

        Args:
            item_ids: List of ids (each starts with ``sh_``) to mark as
                bought. Get the ids from the most recent ``list_shopping``
                output or from an earlier ``add_shopping_items`` return.

        Returns ``ok:bought:N`` with the count actually updated.
        """
        if not user_id:
            return "error: no user_id context"
        if not item_ids:
            return "error: empty item_ids"
        try:
            n = shopping_service.mark_bought(
                tenant_id=tenant_id, user_id=user_id, ids=item_ids
            )
        except Exception:  # noqa: BLE001
            logger.exception("mark_shopping_bought failed")
            return "error: internal"
        return f"ok:bought:{n}"

    @lc_tool
    def remove_shopping_items(item_ids: list[str]) -> str:
        """Remove items from the shopping list (cancel without buying).

        Use when the user says "убери молоко из списка" / "не надо
        хлеб" / "перехотел". Different from mark_shopping_bought —
        these items were never bought. Both move out of the user's view.

        Args:
            item_ids: List of ids to cancel.

        Returns ``ok:removed:N``.
        """
        if not user_id:
            return "error: no user_id context"
        if not item_ids:
            return "error: empty item_ids"
        try:
            n = shopping_service.remove_items(
                tenant_id=tenant_id, user_id=user_id, ids=item_ids
            )
        except Exception:  # noqa: BLE001
            logger.exception("remove_shopping_items failed")
            return "error: internal"
        return f"ok:removed:{n}"

    @lc_tool
    def list_shopping() -> str:
        """List the user's pending (not-yet-bought, not-cancelled)
        shopping items.

        Returns a compact text dump grouped by category with ids so
        subsequent mark_shopping_bought / remove_shopping_items can
        reference rows. Use when the user asks "что в списке?",
        "что покупать?", "сколько осталось?".

        Empty list returns the exact string ``no shopping items``.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            rows = shopping_service.list_pending(
                tenant_id=tenant_id, user_id=user_id
            )
        except Exception:  # noqa: BLE001
            logger.exception("list_shopping failed")
            return "error: internal"
        if not rows:
            return "no shopping items"

        # Group by category in the service's taxonomy order (already sorted).
        lines: list[str] = ["pending shopping items:"]
        current_cat: str | None = None
        for r in rows:
            if r.category != current_cat:
                current_cat = r.category
                lines.append(f"[{current_cat}]")
            qty = f" ({r.quantity_text})" if r.quantity_text else ""
            lines.append(f"  [{r.id}] {r.title}{qty}")
        return "\n".join(lines)

    @lc_tool
    def clear_bought_shopping() -> str:
        """Cancel all items currently in the ``bought`` state — use as
        bulk housekeeping when the user has finished a shopping trip
        and wants a clean list for next time.

        This doesn't affect ``pending`` items; it just moves already-
        checked-off rows out of the history view.

        Returns ``ok:cleared:N``.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            n = shopping_service.clear_bought(
                tenant_id=tenant_id, user_id=user_id
            )
        except Exception:  # noqa: BLE001
            logger.exception("clear_bought_shopping failed")
            return "error: internal"
        return f"ok:cleared:{n}"

    return [
        schedule_reminder,
        list_reminders,
        cancel_reminder,
        onboarding_answered,
        onboarding_deferred,
        onboarding_complete,
        add_shopping_items,
        mark_shopping_bought,
        remove_shopping_items,
        list_shopping,
        clear_bought_shopping,
    ]
