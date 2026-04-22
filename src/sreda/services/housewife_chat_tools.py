"""Housewife chat tools — LangChain tools bound to a tenant/user context.

Exposed to the LLM inside ``execute_conversation_chat`` when the
resolved chat-skill is ``housewife_assistant``. Each tool returns a
short string — the LLM reads it as feedback for the next turn.

Keep tool docstrings descriptive: LangChain's LLM-tool binding uses them
as the tool's specification, so bad docstring = bad tool use.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import tool as lc_tool
from sqlalchemy.orm import Session

from sreda.services.housewife_onboarding import (
    TOPIC_DESCRIPTIONS,
    HousewifeOnboardingService,
)
from sreda.services.housewife_family import HousewifeFamilyService
from sreda.services.housewife_menu import HousewifeMenuService
from sreda.services.housewife_recipes import HousewifeRecipeService
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
    def update_shopping_item(
        item_id: str,
        title: str | None = None,
        quantity_text: str | None = None,
        category: str | None = None,
    ) -> str:
        """Update a single shopping item in place — rename, re-quantify,
        or re-categorise WITHOUT going through remove+add.

        **Use this instead of remove_shopping_items + add_shopping_items
        when the user wants to edit an existing row.** One LLM call
        instead of two saves 5–10 seconds and a chunk of the tool
        budget. Any argument passed as None leaves that field
        untouched — so you can update only the category and keep
        title / quantity as they were.

        Args:
            item_id: id from a recent ``list_shopping`` (``sh_...``).
            title: new title (optional).
            quantity_text: new quantity label (e.g. "2 л", "500 г",
                "по вкусу"). Empty string clears to None.
            category: any string — fixed taxonomy (молочные, мясо_рыба,
                овощи_фрукты, хлеб, бакалея, напитки, готовое,
                замороженное, бытовая_химия, лекарства, другое) OR any
                custom label the user prefers ("специи", "детское
                питание", "канцелярия").

        Returns ``ok:updated:<id>`` or error message.
        """
        if not user_id:
            return "error: no user_id context"
        if not item_id or not item_id.strip():
            return "error: empty item_id"
        try:
            row = shopping_service.update_item(
                tenant_id=tenant_id, user_id=user_id,
                item_id=item_id.strip(),
                title=title, quantity_text=quantity_text,
                category=category,
            )
        except Exception:  # noqa: BLE001
            logger.exception("update_shopping_item failed")
            return "error: internal"
        if row is None:
            return f"error: item {item_id!r} not found"
        return f"ok:updated:{row.id}"

    @lc_tool
    def update_shopping_items_category(
        item_ids: list[str],
        category: str,
    ) -> str:
        """Bulk re-assign category for several shopping items in one call.

        **Use this when the user asks to split/regroup a list**
        ("лекарства отдельно", "все молочные в одну категорию") —
        1 tool call instead of a delete+add cycle per item. Observed
        prod case: LLM regrouped 3 items with list+remove+add = 4 LLM
        iterations / 32 seconds, where this would be list + 1 bulk
        update = 2 iterations / ~12 seconds.

        Args:
            item_ids: list of ids from a recent ``list_shopping``.
            category: target bucket — any string (same contract as
                ``update_shopping_item.category``).

        Returns ``ok:updated:N`` where N is the number of rows
        actually changed (ids that don't exist or belong to other
        tenants are silently skipped).
        """
        if not user_id:
            return "error: no user_id context"
        if not item_ids:
            return "error: empty item_ids"
        if not category or not category.strip():
            return "error: empty category"
        try:
            n = shopping_service.update_items_category(
                tenant_id=tenant_id, user_id=user_id,
                ids=item_ids, category=category,
            )
        except Exception:  # noqa: BLE001
            logger.exception("update_shopping_items_category failed")
            return "error: internal"
        return f"ok:updated:{n}"

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

    # ----------------------------------------------------------------
    # Recipe-book tools (v1.1). Four tools: save, search, get, delete.
    # No explicit "update" — edits = delete + save (keeps the schema
    # simpler and matches what the LLM produces more naturally).
    # ----------------------------------------------------------------
    recipe_service = HousewifeRecipeService(session)

    @lc_tool
    def save_recipe(
        title: str,
        ingredients: list[dict[str, Any]],
        instructions_md: str,
        servings: int,
        source: str,
        source_url: str | None = None,
        tags: list[str] | None = None,
        calories_per_serving: float | None = None,
        protein_per_serving: float | None = None,
        fat_per_serving: float | None = None,
        carbs_per_serving: float | None = None,
    ) -> str:
        """Save ONE recipe to the user's recipe book.

        **For multiple recipes in one turn — use ``save_recipes_batch``
        instead.** Looping save_recipe over N dishes burns the 12-call
        tool budget and leaves the batch half-done.

        Use save_recipe when the user explicitly asks to save a single
        recipe ("сохрани рецепт борща"), when you (the AI) generated
        one recipe the user liked, when you fetched one from the web,
        or when promoting a menu free_text into a structured recipe.
        Always classify the origin via the ``source`` arg — the UI
        shows a badge per source type.

        **Dedup.** Recipe titles are unique per user (case /
        whitespace-insensitive). If a recipe with the same title
        already exists, this returns ``ok:duplicate:<id>`` WITHOUT
        inserting a new row — tell the user the recipe is already
        in the book. Don't call save_recipe with a near-identical
        title variation ("борщ" after already saving "Борщ
        классический") — that's almost certainly the same dish.

        ALWAYS estimate nutrition per serving (kcal + B/Ж/У in grams)
        unless the recipe has no structured ingredients. ±20%
        accuracy from ingredient knowledge is fine for a household
        planner. Skip only if genuinely unknowable (fancy restaurant
        dish with no ingredient list, etc.).

        **Heat level in instructions_md**: for EACH step that involves
        жарку / варку / тушение / запекание, ALWAYS state the fire
        intensity — «на большом огне», «на среднем огне», «на малом
        огне» (или «на медленном огне»). For oven steps — temperature
        in °C ("в духовке при 180°C"). The user cooks by these steps
        and asks "на каком огне?" — don't leave that ambiguous.

        Args:
            title: Short name of the dish. Imperative-free ("Борщ",
                не "Сварить борщ").
            ingredients: list of {title, quantity_text?, is_optional?}.
                title required per item; quantity_text free-form
                ("2 шт", "500 г", "по вкусу").
            instructions_md: step-by-step cooking instructions in
                markdown. Keep concise — bullets or numbered list.
            servings: how many people it feeds (integer). Best-guess
                from ingredient amounts if unclear; default 2.
            source: MUST be one of:
                • "user_dictated" — user narrated the recipe to you
                • "ai_generated" — you invented it during the chat
                • "web_found" — you fetched it from a URL (set
                  source_url too)
                • "upgraded_from_menu" — promoting a free_text menu
                  cell into a full recipe
            source_url: only set when source == "web_found" — the
                origin URL.
            tags: optional short tags like ["суп", "быстрое", "завтрак"].

        Returns status string with the new recipe id.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            recipe, is_new = recipe_service.save_recipe(
                tenant_id=tenant_id,
                user_id=user_id,
                title=title,
                ingredients=ingredients or [],
                instructions_md=instructions_md,
                servings=servings,
                source=source,
                source_url=source_url,
                tags=tags,
                calories_per_serving=calories_per_serving,
                protein_per_serving=protein_per_serving,
                fat_per_serving=fat_per_serving,
                carbs_per_serving=carbs_per_serving,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("save_recipe failed")
            return "error: internal"
        if not is_new:
            # Title already existed for this user — tell the LLM so it
            # can surface "рецепт уже был в книге" to the user instead
            # of claiming it just created one.
            return f"ok:duplicate:{recipe.id}"
        return f"ok:saved:{recipe.id}"

    @lc_tool
    def save_recipes_batch(recipes: list[dict[str, Any]]) -> str:
        """Batch-save multiple recipes to the user's recipe book in one call.

        **Prefer this over calling ``save_recipe`` in a loop** when the
        user asks for many recipes at once ("сохрани 18 рецептов",
        "запиши все рецепты из книги"). Using save_recipe 18 times
        burns the tool-call budget (8 iterations) before you finish.

        Each item in ``recipes`` has the same shape as ``save_recipe``
        args wrapped in a dict:
            {
              "title": "Борщ",
              "ingredients": [{"title": "свёкла", "quantity_text": "2 шт"}, ...],
              "instructions_md": "...",
              "servings": 4,
              "source": "user_dictated",  # or ai_generated / web_found / upgraded_from_menu
              "source_url": null,         # only when source == web_found
              "tags": ["суп"],             # optional
              "calories_per_serving": 320, # optional, ALWAYS estimate
              "protein_per_serving": 12,   # grams; estimate from ingredients
              "fat_per_serving": 8,
              "carbs_per_serving": 45
            }

        Items with empty title or unknown ``source`` are silently
        skipped — the rest of the batch still persists.

        **Heat level in instructions_md**: same rule as save_recipe —
        каждый шаг с термообработкой должен указывать интенсивность
        огня («на большом огне» / «на среднем» / «на малом») или
        температуру духовки в °C. Не оставляй «варить 10 минут» без
        уточнения — пользователь спросит «на каком огне?».

        **Dedup.** The recipe book is unique per user by recipe title
        (case-insensitive, whitespace-insensitive). If the user asks
        "сохрани 10 рецептов" and some are already in the book, they
        will be reported as skipped — NOT re-inserted. Treat trivial
        name variations ("борщ" vs "борщ классический") as the SAME
        recipe and don't bother saving twice; semantic near-duplicates
        ("пельмени" vs "пельмени со сметаной") still go through if
        titles differ — the user will tell you if that's a problem.

        Before a big batch-save where you're unsure what's already
        saved, call ``search_recipes("")`` first to see the book.

        Returns ``ok:batch_saved:N:skipped_as_duplicate:M:ids=[...]``
        where N is newly-created and M is how many were short-circuited
        because the title already existed.
        """
        if not user_id:
            return "error: no user_id context"
        if not recipes:
            return "error: empty batch"
        try:
            outcome = recipe_service.save_recipes_batch(
                tenant_id=tenant_id,
                user_id=user_id,
                recipes=recipes,
            )
        except Exception:  # noqa: BLE001
            logger.exception("save_recipes_batch failed")
            return "error: internal"
        skipped_n = len(outcome.skipped_existing)
        if not outcome.created:
            return f"ok:batch_saved:0:skipped_as_duplicate:{skipped_n}"
        ids_csv = ",".join(r.id for r in outcome.created)
        return (
            f"ok:batch_saved:{len(outcome.created)}:"
            f"skipped_as_duplicate:{skipped_n}:ids=[{ids_csv}]"
        )

    @lc_tool
    def search_recipes(query: str) -> str:
        """Search the user's recipe book by title or tag substring.

        **Returns the WHOLE RECIPE BOOK (all saved recipes), NOT the
        weekly menu.** A recipe being in the book does NOT mean it's
        on the menu for any particular day. To check what the user
        has planned for a specific day — use ``list_menu``. Those are
        different sources of truth:
          - search_recipes → книга (catalog, independent of menu days)
          - list_menu      → план меню на неделю (day-bound cells)

        Use at the start of ``plan_week_menu`` to see what's already
        saved (aim for ≥50% of menu cells pointing at existing
        recipes) and whenever the user says "найди мой рецепт X". Empty
        query returns ALL recipes in reverse-chronological order.

        Args:
            query: search substring (case-insensitive). Empty string
                returns all recipes.

        Returns a compact text dump — one line per recipe with id,
        title, source badge, and first few tags.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            rows = recipe_service.search_recipes(
                tenant_id=tenant_id, user_id=user_id, query=query or "",
            )
        except Exception:  # noqa: BLE001
            logger.exception("search_recipes failed")
            return "error: internal"
        if not rows:
            return "no recipes found"
        lines = [f"{len(rows)} recipe(s):"]
        for r in rows[:50]:
            badge = {
                "user_dictated": "📝",
                "ai_generated": "🤖",
                "web_found": "🌐",
                "upgraded_from_menu": "📅",
            }.get(r.source, "❔")
            tags_blob = ""
            if r.tags_json:
                try:
                    parsed = json.loads(r.tags_json) or []
                    if parsed:
                        tags_blob = f" tags=[{','.join(str(t) for t in parsed[:3])}]"
                except (json.JSONDecodeError, TypeError):
                    pass
            lines.append(f"  [{r.id}] {badge} {r.title}{tags_blob}")
        return "\n".join(lines)

    @lc_tool
    def get_recipe(recipe_id: str) -> str:
        """Fetch full details of a saved recipe (ingredients + instructions).

        Use when you need to reference a recipe in detail — e.g.
        when user asks "как готовить борщ" and you want to quote their
        own saved version, or when pulling ingredients during menu
        generation.

        Args:
            recipe_id: id from search_recipes output (starts with ``rec_``).

        Returns a compact text dump or "error: not found".
        """
        if not user_id:
            return "error: no user_id context"
        try:
            recipe = recipe_service.get_recipe(
                tenant_id=tenant_id, user_id=user_id, recipe_id=recipe_id.strip(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("get_recipe failed")
            return "error: internal"
        if recipe is None:
            return f"error: recipe {recipe_id!r} not found"
        lines = [f"{recipe.title} (on {recipe.servings} servings, source={recipe.source})"]
        if recipe.ingredients:
            lines.append("ingredients:")
            for ing in recipe.ingredients:
                opt = " [optional]" if ing.is_optional else ""
                qty = f" — {ing.quantity_text}" if ing.quantity_text else ""
                lines.append(f"  - {ing.title}{qty}{opt}")
        if recipe.instructions_md:
            lines.append("instructions:")
            lines.append(recipe.instructions_md)
        return "\n".join(lines)

    @lc_tool
    def delete_recipe(recipe_id: str) -> str:
        """Delete a recipe from the user's book. Cascades to ingredients.

        Use sparingly — only when user explicitly asks to remove a
        recipe. To EDIT a recipe, call delete_recipe followed by
        save_recipe with updated data (there's no separate update tool).

        Args:
            recipe_id: id (starts with ``rec_``).

        Returns ``ok:deleted`` or error.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            ok = recipe_service.delete_recipe(
                tenant_id=tenant_id, user_id=user_id, recipe_id=recipe_id.strip(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("delete_recipe failed")
            return "error: internal"
        return "ok:deleted" if ok else f"error: recipe {recipe_id!r} not found"

    # ----------------------------------------------------------------
    # Menu-planning tools (v1.1). Week grid 7×3(+snack) stored as
    # MenuPlan + MenuPlanItem. plan_week_menu is the heavy call; the
    # LLM composes 21 meals in one go, the service just persists.
    # ----------------------------------------------------------------
    menu_service = HousewifeMenuService(session)

    @lc_tool
    def plan_week_menu(
        week_start: str,
        days: list[dict[str, Any]],
        notes: str | None = None,
    ) -> str:
        """Create (or replace) the weekly menu for the user.

        ⚠️ **ПЕРЕЗАПИСЫВАЕТ всю неделю.** Если для week_start уже есть
        план, он ПОЛНОСТЬЮ заменяется переданными днями. Если user
        просит инкрементально добавить ОДИН день (а другие дни
        остаются) — НЕ вызывай plan_week_menu с одним днём, иначе
        сотрёшь остальное. Вместо этого используй ``update_menu_item``
        (по одной ячейке) для точечной вставки.

        Heavy composite call: YOU generate 21 meal cells (7 days ×
        breakfast/lunch/dinner) and pass them as structured data. Before
        calling this, invoke ``search_recipes("")`` to pull the user's
        recipe book — **aim for ≥50% of cells to point at existing
        recipes via recipe_id**. Otherwise the book is bookshelf decoration.

        Priority order when composing meals:
          1. Respect allergies / diet (hard constraint from [ПАМЯТЬ])
          2. Prefer recipe_id over free_text (reuse саved recipes)
          3. Variety — no repeat within 3 days
          4. Practicality — ≤30 min dishes on weekdays, longer OK Sat/Sun
          5. Budget — not steaks daily

        When a meal is free_text and you mention a cooking method,
        include heat level briefly — «на среднем огне» / «в духовке
        при 180°C» — consistent with the save_recipe rule. Avoids
        vague "варить 10 мин" with no idea at what intensity.

        Args:
            week_start: ISO date, any day of the target week. Service
                normalises to Monday automatically.
            days: list of day objects.
                [{"day_of_week": 0,
                  "meals": {
                    "breakfast": {"recipe_id": "rec_..."} | {"free_text": "овсянка с ягодами"},
                    "lunch": {...}, "dinner": {...}, "snack": {...}?
                  }},
                 {"day_of_week": 1, "meals": {...}},
                 ...]
                day_of_week: 0=Mon, 1=Tue, ..., 6=Sun.
                meals values: dict with EITHER ``recipe_id`` OR
                ``free_text`` (not both). Omit meal key to leave it
                empty. ``notes`` optional per cell.
            notes: optional overall note for the week.

        Returns ok:plan_created:<plan_id>:<week_start>.
        """
        if not user_id:
            return "error: no user_id context"

        # Flatten days→cells into the shape HousewifeMenuService expects.
        cells: list[dict[str, Any]] = []
        for day_spec in days or []:
            if not isinstance(day_spec, dict):
                continue
            try:
                day = int(day_spec["day_of_week"])
            except (KeyError, TypeError, ValueError):
                continue
            meals = day_spec.get("meals") or {}
            if not isinstance(meals, dict):
                continue
            for meal_type, meal in meals.items():
                if not isinstance(meal, dict):
                    continue
                cells.append(
                    {
                        "day_of_week": day,
                        "meal_type": meal_type,
                        "recipe_id": meal.get("recipe_id"),
                        "free_text": meal.get("free_text"),
                        "notes": meal.get("notes"),
                    }
                )

        try:
            plan = menu_service.plan_week(
                tenant_id=tenant_id,
                user_id=user_id,
                week_start=week_start,
                cells=cells,
                notes=notes,
            )
        except Exception:  # noqa: BLE001
            logger.exception("plan_week_menu failed")
            return "error: internal"
        return f"ok:plan_created:{plan.id}:{plan.week_start_date.isoformat()}"

    @lc_tool
    def update_menu_item(
        plan_id: str,
        day_of_week: int,
        meal_type: str,
        recipe_id: str | None = None,
        free_text: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Replace a single cell in an existing weekly menu.

        Use for point edits like "замени ужин в среду на пасту". If no
        cell currently exists at (day, meal_type) it's created. Passing
        both recipe_id and free_text as None clears the cell.

        Args:
            plan_id: id from plan_week_menu or list_menu (``menu_...``).
            day_of_week: 0-6 (Mon-Sun).
            meal_type: breakfast | lunch | dinner | snack.
            recipe_id: optional — reuse a saved recipe.
            free_text: optional — ad-hoc dish description.
            notes: optional note for this cell.

        Returns ok:updated or error.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            item = menu_service.update_item(
                tenant_id=tenant_id,
                user_id=user_id,
                plan_id=plan_id.strip(),
                day_of_week=day_of_week,
                meal_type=meal_type,
                recipe_id=(recipe_id or None),
                free_text=(free_text or None),
                notes=(notes or None),
            )
        except ValueError as exc:
            return f"error: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("update_menu_item failed")
            return "error: internal"
        if item is None:
            # Either plan not found OR cell was cleared.
            return "ok:cleared_or_not_found"
        return f"ok:updated:{item.id}"

    @lc_tool
    def list_menu(week_start: str | None = None) -> str:
        """Fetch a weekly menu grid.

        Args:
            week_start: ISO date. If None, returns the user's most
                recent menu across all weeks.

        Returns a compact text dump grouped by day → meal, with recipe
        links shown as [rec_...] ids so get_recipe can follow.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            if week_start:
                plan = menu_service.get_plan_for_week(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    week_start=week_start,
                )
            else:
                plans = menu_service.list_user_plans(
                    tenant_id=tenant_id, user_id=user_id
                )
                plan = plans[0] if plans else None
                # Need items eagerly loaded — fetch by the found week
                if plan is not None:
                    plan = menu_service.get_plan_for_week(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        week_start=plan.week_start_date,
                    )
        except Exception:  # noqa: BLE001
            logger.exception("list_menu failed")
            return "error: internal"
        if plan is None:
            return "no menu plan for that week"

        day_names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        lines = [f"menu plan [{plan.id}] week starting {plan.week_start_date.isoformat()}:"]
        # Group by day
        by_day: dict[int, list] = {}
        for item in plan.items:
            by_day.setdefault(item.day_of_week, []).append(item)
        for d in range(7):
            items = by_day.get(d, [])
            if not items:
                continue
            lines.append(f"  {day_names[d]}:")
            for item in sorted(items, key=lambda x: x.meal_type):
                if item.recipe_id and item.recipe is not None:
                    body = f"[{item.recipe_id}] {item.recipe.title}"
                elif item.free_text:
                    body = item.free_text
                else:
                    continue
                lines.append(f"    {item.meal_type}: {body}")
        return "\n".join(lines)

    # ----------------------------------------------------------------
    # Family member tools (v1.2). Structured household roster used by
    # the shopping scaler and menu planning prompt context. Replaces
    # the loose "состав семьи" free-form AssistantMemory blob.
    #
    # Instantiated here (before generate_shopping_from_menu) so the
    # auto-gen tool can read count_eaters for ingredient scaling.
    # ----------------------------------------------------------------
    family_service = HousewifeFamilyService(session)

    @lc_tool
    def generate_shopping_from_menu(plan_id: str) -> str:
        """Pull all ingredients from a menu plan's recipes into the
        shopping list, scaled to the user's family size.

        Use when user says "добавь ингредиенты меню в список покупок"
        or "собери список покупок на неделю". Ingredients get a
        ``source_recipe_id`` tag so shopping items know which recipe
        they came from (enables future "купил для борща" UX).

        Scaling: the service multiplies every ingredient quantity by
        ``ceil(eaters / recipe.servings)``. ``eaters`` comes from
        ``count_eaters`` on the family-members table (fallback 1 for
        solo users). A 2-serving recipe with a family of 4 doubles
        all quantities. Numeric strings ("500 г") get multiplied;
        free-form ones ("по вкусу") get an ``×N `` prefix so the user
        still sees that scaling was applied. If the user hasn't
        recorded family members yet, call ``add_family_members``
        FIRST so the list is sized correctly.

        Free-text menu cells (no ``recipe_id``) contribute nothing —
        LLM / user add stuff manually for those.

        Args:
            plan_id: id from plan_week_menu / list_menu (``menu_...``).

        Returns ``ok:generated:N:eaters=E`` where N is the items added
        and E is the family-size factor used. ``ok:generated:0`` if
        the plan exists but all cells are free-text.
        """
        if not user_id:
            return "error: no user_id context"
        eaters = family_service.count_eaters(
            tenant_id=tenant_id, user_id=user_id
        )
        try:
            ingredients = menu_service.aggregate_ingredients_for_shopping(
                tenant_id=tenant_id,
                user_id=user_id,
                plan_id=plan_id.strip(),
                eaters_count=eaters,
            )
        except Exception:  # noqa: BLE001
            logger.exception("generate_shopping_from_menu: aggregation failed")
            return "error: internal"

        if not ingredients:
            return "ok:generated:0"

        # Convert recipe-level units (стаканы, ст.л., "по вкусу") into
        # buyable shopping units (литры, граммы, пачки) via LLM. Drops
        # "по вкусу"-style items users already have on hand. Without
        # this step the list becomes nonsense like "молоко 6 стаканов"
        # and "соль по вкусу" — users complained.
        from sreda.services.housewife_shopping_llm import (
            convert_ingredients_to_shopping_list,
        )
        items = convert_ingredients_to_shopping_list(
            ingredients, eaters_count=eaters
        )
        if not items:
            return f"ok:generated:0:eaters={eaters}"
        try:
            rows = shopping_service.add_items(
                tenant_id=tenant_id, user_id=user_id, items=items
            )
        except Exception:  # noqa: BLE001
            logger.exception("generate_shopping_from_menu: add_items failed")
            return "error: internal"
        return f"ok:generated:{len(rows)}:eaters={eaters}"

    @lc_tool
    def add_family_members(members: list[dict[str, Any]]) -> str:
        """Add one or more family members at once.

        **Use this preferentially over single-member adds** — one call
        saves N members, same reason save_recipes_batch beats looping
        save_recipe. Each item is a dict:
            {
              "name": "Маша",
              "role": "self|spouse|child|parent|other",
              "birth_year": 2017,                 # optional
              "age_hint": "8 лет",                 # optional, fallback
              "notes": "аллергия на горчицу"       # optional
            }
        Invalid items (empty name, unknown role, implausible birth_year)
        skipped silently — rest of the batch persists.

        **Dedup by name.** Family members are unique per user by
        normalised name (case-insensitive, whitespace-insensitive).
        If a name already exists it is SKIPPED, not re-inserted —
        don't add the same person twice across turns. If you're
        unsure whether the family is already recorded, call
        ``list_family_members`` first.

        Call ``add_family_members`` when the user says something like
        "у меня жена Катя, сын Никита 10 лет, дочь Маша 8 лет" — LLM
        parses, batches, one tool call.

        Returns ok:added:N:skipped_as_duplicate:M:ids=[fm_...,...].
        M is the count of entries short-circuited because they were
        already in the book.
        """
        if not user_id:
            return "error: no user_id context"
        if not members:
            return "error: empty batch"
        try:
            created = family_service.add_members_batch(
                tenant_id=tenant_id, user_id=user_id, members=members
            )
        except Exception:  # noqa: BLE001
            logger.exception("add_family_members failed")
            return "error: internal"
        skipped = max(0, len(members) - len(created))
        if not created:
            return f"ok:added:0:skipped_as_duplicate:{skipped}"
        ids_csv = ",".join(m.id for m in created)
        return (
            f"ok:added:{len(created)}:skipped_as_duplicate:{skipped}"
            f":ids=[{ids_csv}]"
        )

    @lc_tool
    def list_family_members() -> str:
        """List all recorded household members for the current user.

        Call before ``plan_week_menu`` to know who to cook for, and
        before ``generate_shopping_from_menu`` so shopping scales
        correctly. Also handy when user asks "кто у меня в семье".

        Returns text dump with ids, names, roles, ages, notes.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            rows = family_service.list_members(
                tenant_id=tenant_id, user_id=user_id
            )
        except Exception:  # noqa: BLE001
            logger.exception("list_family_members failed")
            return "error: internal"
        if not rows:
            return "no family members recorded"
        lines = [f"{len(rows)} member(s):"]
        from datetime import datetime as _dt

        this_year = _dt.now().year
        for m in rows:
            age_blob = ""
            if m.birth_year:
                age_blob = f", {this_year - m.birth_year} лет"
            elif m.age_hint:
                age_blob = f", {m.age_hint}"
            notes_blob = f" — {m.notes}" if m.notes else ""
            lines.append(
                f"  [{m.id}] {m.name} ({m.role}{age_blob}){notes_blob}"
            )
        return "\n".join(lines)

    @lc_tool
    def update_family_member(
        member_id: str,
        name: str | None = None,
        role: str | None = None,
        birth_year: int | None = None,
        age_hint: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Update fields of an existing family member.

        Pass None / leave out args you don't want to change. Use when
        user corrects info: "Маше 9 уже" → set birth_year or age_hint;
        "у Никиты аллергия на молоко теперь" → set notes.

        Args:
            member_id: id from list_family_members (``fm_...``).
            name / role / birth_year / age_hint / notes: new values.
                Roles: self | spouse | child | parent | other.

        Returns ok:updated or error.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            row = family_service.update_member(
                tenant_id=tenant_id,
                user_id=user_id,
                member_id=member_id.strip(),
                name=name,
                role=role,
                birth_year=birth_year,
                age_hint=age_hint,
                notes=notes,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("update_family_member failed")
            return "error: internal"
        return "ok:updated" if row else f"error: member {member_id!r} not found"

    @lc_tool
    def remove_family_member(member_id: str) -> str:
        """Delete a family member record. Use only when user explicitly
        says to remove someone (moved out, no longer applicable)."""
        if not user_id:
            return "error: no user_id context"
        try:
            ok = family_service.remove_member(
                tenant_id=tenant_id, user_id=user_id, member_id=member_id.strip()
            )
        except Exception:  # noqa: BLE001
            logger.exception("remove_family_member failed")
            return "error: internal"
        return "ok:removed" if ok else f"error: member {member_id!r} not found"

    @lc_tool
    def clear_menu(week_start: str) -> str:
        """Delete the weekly menu for the given week.

        Use when user says "убери меню", "отмени план на неделю". After
        this, ``list_menu`` for that week returns empty.

        Args:
            week_start: ISO date within the target week. Service
                normalises to Monday.

        Returns ok:cleared:N where N is the number of plans removed.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            n = menu_service.clear_menu(
                tenant_id=tenant_id,
                user_id=user_id,
                week_start=week_start,
            )
        except Exception:  # noqa: BLE001
            logger.exception("clear_menu failed")
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
        update_shopping_item,
        update_shopping_items_category,
        list_shopping,
        clear_bought_shopping,
        save_recipe,
        save_recipes_batch,
        search_recipes,
        get_recipe,
        delete_recipe,
        plan_week_menu,
        update_menu_item,
        list_menu,
        clear_menu,
        generate_shopping_from_menu,
        add_family_members,
        list_family_members,
        update_family_member,
        remove_family_member,
    ]
