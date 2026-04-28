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
from typing import Annotated, Any

from langchain_core.tools import tool as lc_tool
from pydantic import BeforeValidator
from sqlalchemy.orm import Session


def _coerce_to_list(value: Any) -> Any:
    """Pydantic BeforeValidator: если LLM прислала JSON-строку
    типа ``'["a","b"]'`` (что наблюдалось в проде у MiMo для
    save_recipe.tags 50+ раз/неделю) — парсим её в нативный list,
    чтобы pydantic-валидация не упала. Не-строки + не-JSON
    оставляем как есть; ``None`` → ``None``.
    """
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return value


# Type aliases для tool-сигнатур: list-args, которые LLM может
# случайно сериализовать в JSON-строку. BeforeValidator превращает
# строку обратно в list ДО валидации pydantic.
ListOfStr = Annotated[list[str] | None, BeforeValidator(_coerce_to_list)]
ListOfDict = Annotated[
    list[dict[str, Any]], BeforeValidator(_coerce_to_list)
]

from sreda.services.housewife_onboarding import (
    TOPIC_DESCRIPTIONS,
    HousewifeOnboardingService,
)
from sreda.services.housewife_family import HousewifeFamilyService
from sreda.services.housewife_menu import HousewifeMenuService
from sreda.services.housewife_recipes import HousewifeRecipeService
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.checklists import ChecklistService
from sreda.services.housewife_shopping import HousewifeShoppingService
from sreda.services.tasks import TaskService

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
    pending_buttons_state: dict | None = None,
) -> list[Any]:
    """Return LLM tools for the housewife skill, bound to the given
    tenant/user. Called from ``execute_conversation_chat`` when the
    feature_key resolves to ``housewife_assistant``.

    ``pending_buttons_state``: optional mutable dict for the
    ``reply_with_buttons`` tool (Часть 0 плана v2). When LLM calls
    that tool, the (text, buttons) payload is stashed here. The caller
    (``execute_conversation_chat``) reads it after the LLM loop ends
    and converts to an inline keyboard. Pass ``None`` (or omit) to
    disable button support for that turn — then the tool is absent
    from the returned list."""

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

        # 2026-04-23 «баг 2a»: для one-shot напоминаний — не создаём те
        # что уже просрочены. Иначе LLM (которая любит расписывать «в
        # 11:00, 15:00, 20:00 сегодня» при запросе в 20:42) плодит пачку
        # past-due записей, и воркер их выстреливает скопом. Для
        # recurring не трогаем: RRULE сам найдёт следующее будущее
        # срабатывание.
        from sreda.services.housewife_reminders import (
            LATE_FIRE_GRACE_MINUTES,
        )
        if not recurrence_rule:
            now_utc = datetime.now(timezone.utc)
            late_by_min = (now_utc - trigger_at).total_seconds() / 60
            if late_by_min > LATE_FIRE_GRACE_MINUTES:
                return (
                    f"skipped:past:{trigger_iso}:"
                    f"late_by_{int(late_by_min)}min"
                )

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
                For the ``addressing`` topic — ТОЛЬКО короткое имя/ник
                (1-3 слова), БЕЗ префиксов «Пользователя зовут»,
                «Меня зовут», «Пользователь хочет, чтобы его называли».
                Примеры правильного значения: "Борис", "Анна Викторовна",
                "Шеф". Примеры НЕПРАВИЛЬНОГО (запрещены): "Пользователя
                зовут Борис.", "Меня зовут Анна", "Пользователь хочет,
                чтобы его называли «Шеф»". Backend-санитайзер обрежет
                префиксы автоматически, но это последняя линия защиты —
                всё равно передавай чистое имя.

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
        ingredients: ListOfDict,
        instructions_md: str,
        servings: int,
        source: str,
        source_url: str | None = None,
        tags: ListOfStr = None,
        cooking_time_minutes: int | None = None,
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
            cooking_time_minutes: общее время приготовления (от начала
                нарезки до подачи на стол) в МИНУТАХ. Один int, не два
                поля prep+cook. Оценивай по сложности рецепта: салат
                10-15, плов или борщ ~60-90, жаркое в духовке 90-120,
                гуляш 120+. Cap 1..600. Можно None если совсем
                непонятно — но старайся проставлять.

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
                cooking_time_minutes=cooking_time_minutes,
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
    def save_recipes_batch(recipes: ListOfDict) -> str:
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

    # ------------------------------------------------------------------
    # Task scheduler («Расписание») — MVP tools
    # ------------------------------------------------------------------

    task_service = TaskService(session, reminder_service=service)

    def _parse_task_date(raw: str | None) -> "date | None":
        """Parse the date argument accepted by add_task/update_task/list_tasks.
        ``today`` / ``tomorrow`` / ``inbox`` / ISO. Returns None for
        inbox or unparseable input (caller decides whether to error)."""
        from datetime import date, datetime as _dt, timedelta as _td

        if raw is None:
            return None
        s = raw.strip().lower()
        if not s or s == "inbox":
            return None
        if s == "today":
            return _dt.now(timezone.utc).date()
        if s == "tomorrow":
            return _dt.now(timezone.utc).date() + _td(days=1)
        try:
            return _dt.fromisoformat(s).date()
        except ValueError:
            return None

    def _parse_hhmm(raw: str | None) -> "time | None":
        from datetime import time as _t

        if not raw:
            return None
        s = raw.strip()
        if not s:
            return None
        # Accept "07:00" or "7:00" or full ISO "07:00:00"
        try:
            parts = s.split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            return _t(hour=h, minute=m)
        except (ValueError, IndexError):
            return None

    def _fmt_task_for_llm(t: Any) -> str:
        """Compact one-liner for list_tasks output."""
        bits: list[str] = [f"[{t.id}]"]
        bits.append(t.title)
        when: list[str] = []
        if t.scheduled_date:
            when.append(t.scheduled_date.isoformat())
        if t.time_start:
            hhmm = t.time_start.strftime("%H:%M")
            if t.time_end:
                hhmm += "–" + t.time_end.strftime("%H:%M")
            when.append(hhmm)
        if when:
            bits.append("on " + " ".join(when))
        if t.recurrence_rule:
            bits.append(f"recurring={t.recurrence_rule}")
        if t.reminder_id:
            bits.append(
                f"reminder=за {t.reminder_offset_minutes or 0}мин"
            )
        if t.status != "pending":
            bits.append(f"status={t.status}")
        # 2026-04-27 fix (прод-баг): юзер сохранял в notes детали кроя
        # («Лайм — простыня 140×200×20, пододеяльник 140×200, наволочки
        # 50×70»), потом спрашивал бота «что по лайму?» — LLM отвечала
        # «нет данных», потому что list_tasks отдавал только title без
        # notes. Mini App в _task_dict notes отдаёт. Догоняем формат.
        if t.notes:
            bits.append(f"notes={t.notes}")
        return " · ".join(bits)

    @lc_tool
    def add_task(
        title: str,
        scheduled_date: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        recurrence_rule: str | None = None,
        notes: str | None = None,
        reminder_offset_minutes: int | None = None,
    ) -> str:
        """Create a task in the user's planner (Расписание).

        Use for any "поставь задачу X", "добавь Y в расписание",
        "запиши на завтра Z". Map the user's phrasing into:

        Args:
            title: short name ("утренняя разминка", "встреча с врачом")
            scheduled_date: one of ``"today"``, ``"tomorrow"``, an ISO
                date ``"2026-04-25"``, ``"inbox"`` / null for an
                undated task (not shown on today-view).
            time_start: local time ``"HH:MM"`` (e.g. ``"07:00"`` = 7 AM
                in the user's timezone). Optional.
            time_end: local time, optional — "07:00–07:30" uses both.
            recurrence_rule: RFC 5545 RRULE, UTC-anchored BYHOUR like
                ``schedule_reminder``. Example ``каждый будний день
                7 утра MSK`` → ``"FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;
                BYHOUR=4;BYMINUTE=0"`` (MSK−3 = UTC).
            notes: optional free-form note.
            reminder_offset_minutes: minutes BEFORE time_start to ping
                the user. Pass only when the user EXPLICITLY asks for
                a reminder at creation time ("с напоминанием за 15
                минут"). Otherwise leave null and ASK the user AFTER
                the task is created.

        Returns ``ok:created:task_<id>`` (possibly with ``+reminder``
        suffix) or ``error:<reason>``.
        """
        if not user_id:
            return "error: no user_id context"
        date_obj = _parse_task_date(scheduled_date)
        t_start = _parse_hhmm(time_start)
        t_end = _parse_hhmm(time_end)
        # reminder requires a schedule
        if reminder_offset_minutes is not None and (not date_obj or not t_start):
            return (
                "error: reminder requires scheduled_date + time_start; "
                "re-call without reminder_offset_minutes and ask user"
            )
        try:
            task = task_service.add(
                tenant_id=tenant_id,
                user_id=user_id,
                title=title,
                scheduled_date=date_obj,
                time_start=t_start,
                time_end=t_end,
                recurrence_rule=recurrence_rule,
                notes=notes,
                reminder_offset_minutes=reminder_offset_minutes,
            )
        except ValueError as exc:
            return f"error:{exc}"
        except Exception:  # noqa: BLE001
            logger.exception("add_task failed")
            return "error: internal"
        suffix = ""
        if task.reminder_id:
            suffix = f":reminder=за {task.reminder_offset_minutes}мин"
        return f"ok:created:{task.id}{suffix}"

    @lc_tool
    def list_tasks(date: str = "today", status: str = "pending") -> str:
        """List tasks filtered by date and status.

        Args:
            date: ``"today"`` (default) / ``"tomorrow"`` / ISO date /
                ``"inbox"`` (tasks without a date) / ``"all"`` (no
                date filter).
            status: ``"pending"`` (default) / ``"completed"`` /
                ``"all"``.

        Returns a text listing with task ids the LLM can pass to
        complete_task / update_task / cancel_task. Empty set →
        ``"no tasks"``.
        """
        if not user_id:
            return "error: no user_id context"
        s = (date or "today").strip().lower()
        if s == "all":
            # No date filter — returns every row regardless of
            # scheduled_date (both dated and inbox tasks).
            date_obj = None
            include_no_date = False
        elif s == "inbox":
            # Only rows with scheduled_date IS NULL.
            date_obj = None
            include_no_date = True
        else:
            date_obj = _parse_task_date(s)
            include_no_date = False

        status_arg: str | None
        if status == "all":
            status_arg = None
        else:
            status_arg = status

        # For inbox specifically, we want ONLY no-date rows; list()
        # treats scheduled_date=None + include_no_date=True as that.
        if s == "inbox":
            rows = task_service.list(
                tenant_id=tenant_id, user_id=user_id,
                scheduled_date=None, include_no_date=True,
                status=status_arg,
            )
        elif date_obj is not None and status_arg == "pending":
            # Specific date + pending status → use RRULE-aware path so
            # a daily task created on day N surfaces on day N+k too.
            # Regression 2026-04-23: base list() returns only rows where
            # scheduled_date == date_obj, missing all recurring
            # expansions. list_today() handles that correctly.
            rows = task_service.list_today(
                tenant_id=tenant_id, user_id=user_id,
                today=date_obj,
            )
        else:
            rows = task_service.list(
                tenant_id=tenant_id, user_id=user_id,
                scheduled_date=date_obj, include_no_date=include_no_date,
                status=status_arg,
            )
        if not rows:
            return "no tasks"
        return "\n".join(_fmt_task_for_llm(t) for t in rows)

    @lc_tool
    def update_task(
        task_id: str,
        title: str | None = None,
        scheduled_date: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        recurrence_rule: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Patch an existing task. Only pass fields that changed.

        Changes to ``scheduled_date`` / ``time_start`` automatically
        reschedule the linked reminder (if any). Use
        ``attach_reminder`` / ``detach_reminder`` to toggle the
        reminder itself.
        """
        if not user_id:
            return "error: no user_id context"
        date_obj = _parse_task_date(scheduled_date) if scheduled_date is not None else None
        t_start = _parse_hhmm(time_start) if time_start is not None else None
        t_end = _parse_hhmm(time_end) if time_end is not None else None
        try:
            task = task_service.update(
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id.strip(),
                title=title,
                scheduled_date=date_obj,
                time_start=t_start,
                time_end=t_end,
                recurrence_rule=recurrence_rule,
                notes=notes,
            )
        except Exception:  # noqa: BLE001
            logger.exception("update_task failed")
            return "error: internal"
        if task is None:
            return f"error: task {task_id!r} not found"
        return f"ok:updated:{task.id}"

    @lc_tool
    def complete_task(task_id: str) -> str:
        """Mark a task as done. For one-shot tasks with a linked
        reminder, the reminder gets cancelled automatically (no ping
        for something already finished). Recurring tasks keep their
        reminder active — tomorrow's occurrence still fires."""
        if not user_id:
            return "error: no user_id context"
        try:
            task = task_service.complete(
                tenant_id=tenant_id, user_id=user_id, task_id=task_id.strip(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("complete_task failed")
            return "error: internal"
        if task is None:
            return f"error: task {task_id!r} not found"
        return f"ok:completed:{task.id}"

    @lc_tool
    def uncomplete_task(task_id: str) -> str:
        """Restore a completed task to pending. The linked reminder,
        if it got cancelled on completion, is NOT brought back — the
        user can call ``attach_reminder`` to re-add one."""
        if not user_id:
            return "error: no user_id context"
        try:
            task = task_service.uncomplete(
                tenant_id=tenant_id, user_id=user_id, task_id=task_id.strip(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("uncomplete_task failed")
            return "error: internal"
        if task is None:
            return f"error: task {task_id!r} not found"
        return f"ok:uncompleted:{task.id}"

    @lc_tool
    def cancel_task(task_id: str) -> str:
        """Soft-cancel a task — row stays in DB with status=cancelled,
        disappears from pending lists. Cancels the linked reminder
        if any. Use when user says "отмени задачу X" without asking
        to fully delete it."""
        if not user_id:
            return "error: no user_id context"
        try:
            task = task_service.cancel(
                tenant_id=tenant_id, user_id=user_id, task_id=task_id.strip(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("cancel_task failed")
            return "error: internal"
        if task is None:
            return f"error: task {task_id!r} not found"
        return f"ok:cancelled:{task.id}"

    @lc_tool
    def delete_task(task_id: str) -> str:
        """Hard-delete a task (row gone from DB). Cancels the linked
        reminder if any. Use when user says "убери совсем", "удали"."""
        if not user_id:
            return "error: no user_id context"
        try:
            ok = task_service.delete(
                tenant_id=tenant_id, user_id=user_id, task_id=task_id.strip(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("delete_task failed")
            return "error: internal"
        return "ok:deleted" if ok else f"error: task {task_id!r} not found"

    @lc_tool
    def attach_reminder(task_id: str, offset_minutes: int) -> str:
        """Attach a reminder to an already-created task. Use when the
        user answered the post-creation "нужно ли напоминание?"
        question with "да, за N минут". Requires the task to have a
        scheduled date + time_start (can't remind for inbox tasks)."""
        if not user_id:
            return "error: no user_id context"
        if not isinstance(offset_minutes, int) or offset_minutes <= 0:
            return "error: offset_minutes must be a positive integer"
        try:
            task = task_service.attach_reminder(
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id.strip(),
                offset_minutes=offset_minutes,
            )
        except ValueError as exc:
            return f"error:{exc}"
        except Exception:  # noqa: BLE001
            logger.exception("attach_reminder failed")
            return "error: internal"
        if task is None:
            return f"error: task {task_id!r} not found"
        return f"ok:reminder_attached:{task.reminder_id}:за {offset_minutes}мин"

    @lc_tool
    def detach_reminder(task_id: str) -> str:
        """Remove the reminder from a task (cancels the underlying
        FamilyReminder). Use when user says "убери напоминание с
        этой задачи"."""
        if not user_id:
            return "error: no user_id context"
        try:
            task = task_service.detach_reminder(
                tenant_id=tenant_id, user_id=user_id, task_id=task_id.strip(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("detach_reminder failed")
            return "error: internal"
        if task is None:
            return f"error: task {task_id!r} not found"
        return "ok:reminder_detached"

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

    # ------------------------------------------------------------------
    # reply_with_buttons (Часть 0 плана v2 — inline-кнопки для ответов)
    # ------------------------------------------------------------------
    @lc_tool
    def reply_with_buttons(text: str, buttons: list[str]) -> str:
        """Отправить ответ с 2-4 inline-кнопками-вариантами.

        ИСПОЛЬЗУЙ ОБЯЗАТЕЛЬНО, если твой ответ содержит вопрос к юзеру.
        Кнопки — короткие (≤20 символов) реплики, которые юзер мог бы
        сам написать в ответ. Никаких «Да/Нет» — всегда конкретика:
        вместо «Да» → «Да, собери меню», вместо «Нет» → «Не сейчас».

        Примеры корректных вызовов:
        - text="Помню у Пети безлактозная. Собрать меню?",
          buttons=["Да, собери", "Не сейчас", "Покажи список блюд"]
        - text="Кто идёт к врачу?",
          buttons=["Петя — к педиатру", "Маша — к ортодонту", "Другое"]

        Не добавляй пустых действий («Отмена», «Назад») — Telegram
        даёт стандартный back-button сам.

        Если вопроса в ответе нет — НЕ вызывай этот тул, отвечай
        обычным текстом.

        Args:
            text: Текст сообщения пользователю.
            buttons: 2-4 короткие реплики-варианта ответа.

        Returns: ok:buttons:<N> где N — число принятых кнопок.
        """
        if pending_buttons_state is None:
            # Caller не передал state — тул не работает, возвращаем
            # ошибку, чтобы LLM в логах это увидела и ответила без
            # кнопок на следующей итерации.
            return "error: buttons disabled in this context"
        clean = [b for b in (buttons or []) if b and b.strip()]
        clean = clean[:4]
        if len(clean) < 2:
            return "error: need at least 2 buttons"
        pending_buttons_state["text"] = (text or "").strip()
        pending_buttons_state["buttons"] = clean
        return f"ok:buttons:{len(clean)}"

    # ------------------------------------------------------------------
    # Checklists — именованные списки дел с галочками (план 2026-04-25)
    # ------------------------------------------------------------------
    checklist_service = ChecklistService(session)

    @lc_tool
    def create_checklist(title: str) -> str:
        """Create a named checklist (todo-list with checkboxes).

        Use when the user dictates a NAMED list of items WITHOUT
        specific dates — e.g. «План кроя на эту неделю», «Дела на дачу»,
        «Сборы в школу». NOT for shopping (use add_shopping_items)
        and NOT for events with dates (use add_task).

        Args:
            title: short list name (≤200 chars), e.g. «План кроя на эту неделю».

        Returns: ``ok:created:checklist_<id>:<title>`` or ``error:<msg>``.
        Pair with ``add_checklist_items`` next call to populate.
        """
        if not user_id:
            return "error: no user_id context"
        try:
            cl = checklist_service.create_list(
                tenant_id=tenant_id, user_id=user_id, title=title,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("create_checklist failed")
            return "error: internal"
        return f"ok:created:{cl.id}:{cl.title}"

    @lc_tool
    def add_checklist_items(list_id_or_title: str, items: list[str]) -> str:
        """Add items to an existing checklist (or create one if missing).

        Resolves ``list_id_or_title`` either by exact id (``checklist_*``)
        or fuzzy title match against active lists. If no match — creates
        a NEW checklist with that title and adds items there.

        Args:
            list_id_or_title: id like ``checklist_xxx`` OR a short title
                that fuzzy-matches an existing active list (e.g. «План кроя»).
            items: list of item titles, e.g. ["Лаванда 298 ТС, простыня
                141×200×19", "Шампань страйп, простыня 202×204×26"].

        Returns: ``ok:added:N:list=<id>`` or ``error:<msg>``.
        """
        if not user_id:
            return "error: no user_id context"
        if not items or not any((i or "").strip() for i in items):
            return "error: empty items"

        cl = checklist_service.find_list_by_title(
            tenant_id=tenant_id, user_id=user_id, needle=list_id_or_title,
        )
        if cl is None:
            try:
                cl = checklist_service.create_list(
                    tenant_id=tenant_id, user_id=user_id,
                    title=list_id_or_title,
                )
            except ValueError as exc:
                return f"error: {exc}"
            except Exception:  # noqa: BLE001
                logger.exception("add_checklist_items: implicit create failed")
                return "error: internal"
        try:
            added, skipped_dup = checklist_service.add_items(
                list_id=cl.id, items=items
            )
        except Exception:  # noqa: BLE001
            logger.exception("add_checklist_items failed")
            return "error: internal"
        # 2026-04-28: возвращаем и dup count чтобы LLM мог сказать
        # юзеру «3 пункта добавлены, 2 уже были» вместо тихого
        # удвоения (incident tg_634496616 — move task создал дубль).
        if skipped_dup:
            return (
                f"ok:added:{len(added)}:dups:{len(skipped_dup)}:"
                f"list={cl.id}"
            )
        return f"ok:added:{len(added)}:list={cl.id}"

    @lc_tool
    def move_task_to_checklist(
        task_id: str, list_id_or_title: str
    ) -> str:
        """Atomically move a task from schedule to a checklist.

        ОДИН вызов вместо двух (cancel_task + add_checklist_items) —
        безопаснее, так как невозможно «потерять» task частично или
        задвоить пункт. Используй когда:

        * Юзер просит «перенеси X из расписания в дела/чек-лист Y»
        * Понял что в прошлом turn'е ошибочно создал task вместо
          add_checklist_items, и хочешь перенести
        * Юзер уточнил «это не на конкретное время — переложи в дела»

        Что делает (атомарно, в одной транзакции):
        1. Cancel task (status='cancelled' — soft delete, audit
           trail сохраняется, retention worker подчистит позже)
        2. Если у task'а был reminder — он тоже отменяется
        3. Add item с title задачи в target checklist (с dedup
           защитой — если такой пункт уже есть, не задвоит)
        4. Если target checklist не найден — создаёт его с этим title

        Args:
            task_id: id задачи (формат ``task_*``).
            list_id_or_title: target checklist — либо id
                (``checklist_*``), либо title (fuzzy-match по
                существующим, например «Глобальные дела на даче»).

        Returns:
            ``ok:moved:item_id=clitem_xxx:list=checklist_yyy``
            ``ok:moved:item_id=existing:list=...:dup`` (если уже было)
            ``error:task_not_found`` / ``error:list_resolve_failed``
        """
        if not user_id:
            return "error: no user_id context"

        # Step 1: cancel task (with ownership check via _get inside)
        try:
            task = task_service.cancel(
                tenant_id=tenant_id, user_id=user_id, task_id=task_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("move_task_to_checklist: cancel failed")
            return "error: internal_cancel"
        if task is None:
            return "error: task_not_found"

        # Сохраняем title пока task ещё доступен (EncryptedString
        # decrypted via ORM read).
        task_title = (task.title or "").strip()
        if not task_title:
            return "error: task_has_empty_title"

        # Step 2: resolve target checklist
        cl = checklist_service.find_list_by_title(
            tenant_id=tenant_id, user_id=user_id, needle=list_id_or_title,
        )
        if cl is None:
            try:
                cl = checklist_service.create_list(
                    tenant_id=tenant_id, user_id=user_id,
                    title=list_id_or_title,
                )
            except ValueError as exc:
                return f"error: {exc}"
            except Exception:  # noqa: BLE001
                logger.exception("move_task_to_checklist: create_list failed")
                return "error: list_resolve_failed"

        # Step 3: add item (with dedup)
        try:
            created, skipped = checklist_service.add_items(
                list_id=cl.id, items=[task_title]
            )
        except Exception:  # noqa: BLE001
            logger.exception("move_task_to_checklist: add_items failed")
            return "error: internal_add"

        if created:
            return f"ok:moved:item_id={created[0].id}:list={cl.id}"
        if skipped:
            return f"ok:moved:item_id=existing:list={cl.id}:dup"
        # Не должно дойти сюда — items был непуст
        return "error: nothing_added"

    @lc_tool
    def list_checklists() -> str:
        """List all active checklists with item counts (pending/done/total).

        Use when user asks «какие у меня списки», «покажи все мои планы»,
        «что у меня в чек-листах». Returns one line per checklist with
        id+title+counts. Empty → ``no checklists``.
        """
        if not user_id:
            return "error: no user_id context"
        rows = checklist_service.list_active(
            tenant_id=tenant_id, user_id=user_id,
        )
        if not rows:
            return "no checklists"
        lines = []
        for cl in rows:
            p, d, t = checklist_service.list_summary(list_id=cl.id)
            lines.append(f"[{cl.id}] · {cl.title} · {p} pending, {d} done, {t} total")
        return "\n".join(lines)

    @lc_tool
    def show_checklist(list_id_or_title: str) -> str:
        """Show items inside one checklist with their status (pending/done).

        Use when user asks «покажи план кроя», «что осталось в списке X»,
        «что я ещё не сделал из плана». Resolves list by id or fuzzy title.

        Args:
            list_id_or_title: ``checklist_xxx`` id or fuzzy title fragment.

        Returns: multi-line listing or ``error:not_found:<needle>``.
        Format per item: ``[<id>] ☐ <title>`` (pending) or ``☑`` (done).
        """
        if not user_id:
            return "error: no user_id context"
        cl = checklist_service.find_list_by_title(
            tenant_id=tenant_id, user_id=user_id, needle=list_id_or_title,
        )
        if cl is None:
            return f"error: not_found: {list_id_or_title!r}"
        items = checklist_service.list_items(list_id=cl.id)
        if not items:
            return f"empty: list={cl.id} title={cl.title!r}"
        lines = [f"# {cl.title} ({cl.id})"]
        for it in items:
            mark = {"pending": "☐", "done": "☑", "cancelled": "✗"}.get(
                it.status, "?"
            )
            lines.append(f"[{it.id}] {mark} {it.title}")
        return "\n".join(lines)

    @lc_tool
    def mark_checklist_item_done(
        list_id_or_title: str, item_title_match: str,
    ) -> str:
        """Mark one item inside a checklist as done.

        Use when user says «закройила лаванду», «купила сахар»,
        «сделал X» — find the matching pending item and flip status.

        Args:
            list_id_or_title: which list to look in (id or fuzzy title).
            item_title_match: substring of the item to mark done.

        Returns: ``ok:done:<item_id>:<title>`` or ``error:not_found``.
        """
        if not user_id:
            return "error: no user_id context"
        cl = checklist_service.find_list_by_title(
            tenant_id=tenant_id, user_id=user_id, needle=list_id_or_title,
        )
        if cl is None:
            return f"error: list_not_found: {list_id_or_title!r}"
        item = checklist_service.find_item_by_title(
            list_id=cl.id, needle=item_title_match,
        )
        if item is None:
            return f"error: item_not_found: {item_title_match!r}"
        done = checklist_service.mark_done(item_id=item.id)
        if done is None:
            return "error: internal"
        return f"ok:done:{done.id}:{done.title}"

    @lc_tool
    def delete_checklist_item(
        list_id_or_title: str, item_title_match: str,
    ) -> str:
        """Hard-delete one item from a checklist (item is GONE).

        Use when user says «удали пункт X», «убери из списка Y»,
        «не то записала, удали» — pure correction, item disappears
        from all views. Differs from mark_checklist_item_done (status=
        done, ☑) and from archive_checklist (whole list out of view).

        Use this in particular when YOU (the AI) misheard / wrote a
        wrong item earlier in this conversation and the user asks
        to remove that wrong record — the previous bad item is gone,
        only the corrected one stays.

        Args:
            list_id_or_title: id or fuzzy title of the checklist.
            item_title_match: substring of the item title to remove.

        Returns: ``ok:deleted:<item_id>:<title>`` or ``error:not_found``.
        """
        if not user_id:
            return "error: no user_id context"
        cl = checklist_service.find_list_by_title(
            tenant_id=tenant_id, user_id=user_id, needle=list_id_or_title,
        )
        if cl is None:
            return f"error: list_not_found: {list_id_or_title!r}"
        item = checklist_service.find_item_by_title(
            list_id=cl.id, needle=item_title_match,
            only_pending=False,  # ищем по всем — может удалять и done
        )
        if item is None:
            return f"error: item_not_found: {item_title_match!r}"
        item_id = item.id
        item_title = item.title
        ok = checklist_service.delete_item(item_id=item_id)
        if not ok:
            return "error: internal"
        return f"ok:deleted:{item_id}:{item_title}"

    @lc_tool
    def archive_checklist(list_id_or_title: str) -> str:
        """Archive a checklist (hide from active lists, keep in DB).

        Use when user says «закрой список X», «убери план кроя»,
        «архивируй» — checklist disappears from list_checklists и Mini App,
        но pages не удаляется (для recall истории).

        Returns: ``ok:archived:<id>`` or ``error:not_found``.
        """
        if not user_id:
            return "error: no user_id context"
        cl = checklist_service.find_list_by_title(
            tenant_id=tenant_id, user_id=user_id, needle=list_id_or_title,
        )
        if cl is None:
            return f"error: not_found: {list_id_or_title!r}"
        archived = checklist_service.archive_list(
            tenant_id=tenant_id, user_id=user_id, list_id=cl.id,
        )
        if archived is None:
            return "error: internal"
        return f"ok:archived:{archived.id}"

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
        # Task scheduler («Расписание») — v1 MVP (2026-04-22)
        add_task,
        list_tasks,
        update_task,
        complete_task,
        uncomplete_task,
        cancel_task,
        delete_task,
        attach_reminder,
        detach_reminder,
        # Inline-кнопки (Часть 0 плана v2).
        reply_with_buttons,
        # Чек-листы — именованные списки дел с галочками (план 2026-04-25)
        create_checklist,
        add_checklist_items,
        list_checklists,
        show_checklist,
        mark_checklist_item_done,
        delete_checklist_item,
        archive_checklist,
        # 2026-04-28: атомарный перенос task → checklist
        move_task_to_checklist,
    ]
