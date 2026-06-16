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
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from langchain_core.tools import tool as lc_tool
from pydantic import BeforeValidator
from sqlalchemy.orm import Session


# R-30 (2026-05-15): anti-confab guard для всех mutating tools. Префиксуется
# к docstring каждого write-tool через ``_write_lc_tool`` — попадает в
# ``description`` поле LangChain Tool spec'а, видится LLM'ом перед каждым
# вызовом. Trigger: trace_1be1a4c0f576464e — mimo вызвала
# ``add_checklist_items × 8`` на чистом read-intent «Покажи мне весь крой
# на сегодня». R-30 — точечный hotfix до R-20 Stage 1 plan'а.
#
# Wording оптимизирован под mimo-v2.5-pro: явный negative example
# (read-verbs НЕ повод писать), explicit fallback («если сомневаешься —
# спроси»), русский текст под русскоязычный prompt контекст. Размер
# ~350 chars × 36 write-tools = ~12KB добавки к tool spec — приемлемо
# на mimo 1M context window. После R-20 Stage 1 deploy можно откатить
# (guard станет редундантным с final-output validator'ом).
WRITE_GUARD = (
    "[WRITE-TOOL] Вызывай ТОЛЬКО когда пользователь ЯВНО выразил намерение "
    "что-то записать/добавить/сохранить/создать/изменить/удалить/отменить/"
    "напомнить/запланировать/отметить/перенести/вычеркнуть — например: "
    "«добавь», «запиши», «поставь», «сохрани», «создай», «занеси», "
    "«обнови», «измени», «удали», «отмени», «напомни», «запланируй», "
    "«отметь», «перенеси», «вычеркни» и подобные write-глаголы. Список "
    "иллюстративный, НЕ исчерпывающий — определяй intent по смыслу, не по "
    "точному совпадению слова. Read-команды («покажи», «посмотри», "
    "«что у меня в...», «какие...», «есть ли», «прочитай») НЕ являются "
    "поводом вызывать write-tools. Если у пользователя read-намерение и "
    "список пустой или неполный — отвечай ТЕКСТОМ, НЕ заполняй сам списки "
    "prior knowledge'ом. Если сомневаешься — спроси «записать?» сначала."
)


def _write_lc_tool(fn):
    """R-30 write-tool wrapper. Prepends ``WRITE_GUARD`` to the function's
    docstring (which LangChain's ``@tool`` uses as the description passed
    to the LLM), then delegates to ``lc_tool``.

    Применяется к каждому mutating tool в ``build_housewife_tools``.
    Read-only tools (``list_*``, ``show_*``, ``get_*``, ``search_*``,
    ``reply_with_buttons``) остаются на ``@lc_tool`` без guard'а.
    """
    if fn.__doc__:
        fn.__doc__ = WRITE_GUARD + "\n\n" + fn.__doc__
    else:
        fn.__doc__ = WRITE_GUARD
    return lc_tool(fn)


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
    TOPIC_ORDER,
    HousewifeOnboardingService,
)
from sreda.services.housewife_family import HousewifeFamilyService
from sreda.services.housewife_menu import HousewifeMenuService
from sreda.services.housewife_recipes import HousewifeRecipeService
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.checklists import ChecklistService
from sreda.services.housewife_shopping import HousewifeShoppingService
from sreda.services.tasks import TaskService
from sreda.services.tool_schemas.tool_ok_codec import encode_tool_ok  # #115

logger = logging.getLogger(__name__)


def _title_matches(needle: str | None, hay: str | None) -> bool:
    """#122: подстрочное совпадение названий без учёта регистра и ё.

    Для «удали/отметь X по имени»: читающие инструменты фильтруют список
    этим помощником, дальше штатный ``.only`` выбирает ровно-один (или даёт
    честное уточнение из УЖЕ отфильтрованных кандидатов). Пустой/None
    needle = «без фильтра» (обратная совместимость планов без аргумента)."""
    if not needle:
        return True

    def _norm(s: str | None) -> str:
        return " ".join(str(s or "").casefold().replace("ё", "е").split())

    return _norm(needle) in _norm(hay)


_MENU_DAY_NAMES_RU = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)
_MENU_MONTH_NAMES_RU = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_MENU_MEAL_LABELS_RU = {
    "breakfast": "Завтрак",
    "lunch": "Обед",
    "dinner": "Ужин",
    "snack": "Перекус",
}
_MENU_MEAL_ORDER = {
    "breakfast": 0,
    "lunch": 1,
    "dinner": 2,
    "snack": 3,
}


def _format_menu_date_ru(value: Any) -> str:
    return f"{value.day} {_MENU_MONTH_NAMES_RU[value.month]}"


def _format_menu_range_ru(week_start: Any) -> str:
    week_end = week_start + timedelta(days=6)
    if week_start.month == week_end.month:
        return (
            f"{week_start.day}–{week_end.day} "
            f"{_MENU_MONTH_NAMES_RU[week_start.month]}"
        )
    return f"{_format_menu_date_ru(week_start)} – {_format_menu_date_ru(week_end)}"


def _menu_item_body(item: Any, *, include_recipe_id: bool) -> str | None:
    if item.recipe_id and item.recipe is not None:
        if include_recipe_id:
            return f"[{item.recipe_id}] {item.recipe.title}"
        return item.recipe.title
    if item.free_text:
        return item.free_text
    return None


def _iter_menu_plan_day_lines(
    plan: Any,
    *,
    include_recipe_ids: bool,
    bullet_meals: bool,
) -> list[str]:
    lines: list[str] = []
    by_day: dict[int, list] = {}
    for item in plan.items:
        by_day.setdefault(item.day_of_week, []).append(item)
    for d in range(7):
        items = by_day.get(d, [])
        if not items:
            continue
        day_date = plan.week_start_date + timedelta(days=d)
        lines.append(f"{_MENU_DAY_NAMES_RU[d]}, {_format_menu_date_ru(day_date)}")
        for item in sorted(
            items,
            key=lambda x: _MENU_MEAL_ORDER.get(x.meal_type, 99),
        ):
            body = _menu_item_body(item, include_recipe_id=include_recipe_ids)
            if body is None:
                continue
            meal_label = _MENU_MEAL_LABELS_RU.get(item.meal_type, item.meal_type)
            prefix = f"• {meal_label}" if bullet_meals else meal_label
            lines.append(f"{prefix}: {body}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _format_menu_plan_for_user(plan: Any) -> str:
    lines = [f"Меню на неделю {_format_menu_range_ru(plan.week_start_date)}:"]
    day_lines = _iter_menu_plan_day_lines(
        plan,
        include_recipe_ids=False,
        bullet_meals=True,
    )
    if day_lines:
        lines.extend(["", *day_lines, "", "Собрать список покупок?"])
    else:
        lines.append("")
        lines.append("Пока пусто.")
    return "\n".join(lines)


_BYDAY_INDEX = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_WEEKDAY_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")  # Mon..Sun
# INTERVAL == 1 → fixed adverb.
_FREQ_EVERY1_RU = {
    "MINUTELY": "ежеминутно", "HOURLY": "ежечасно", "DAILY": "ежедневно",
    "WEEKLY": "еженедельно", "MONTHLY": "ежемесячно", "YEARLY": "ежегодно",
}
# INTERVAL > 1 → «каждые N <unit>» — (one, few, many) plural forms.
_FREQ_UNIT_RU = {
    "MINUTELY": ("минуту", "минуты", "минут"),
    "HOURLY": ("час", "часа", "часов"),
    "DAILY": ("день", "дня", "дней"),
    "WEEKLY": ("неделю", "недели", "недель"),
    "MONTHLY": ("месяц", "месяца", "месяцев"),
    "YEARLY": ("год", "года", "лет"),
}
# Sub-hour frequencies have no meaningful clock — don't append «в HH:MM».
_SUBHOUR_FREQ = {"MINUTELY", "HOURLY"}


def _plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Russian plural: forms = (one, few, many). 1→one, 2-4→few, else many
    (with the 11-14 exception that always takes ``many``)."""
    one, few, many = forms
    if 11 <= n % 100 <= 14:
        return many
    tail = n % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _parse_rrule(rrule: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for tok in rrule.split(";"):
        if "=" in tok:
            k, v = tok.split("=", 1)
            parts[k.strip().upper()] = v.strip()
    return parts


def _recurrence_phrase(rrule: str, day_delta: int) -> str | None:
    """RRULE → short Russian phrase for the COMMON, unambiguous cases; ``None``
    for anything else, so the caller falls back to a safe «по расписанию,
    ближайшее — <next local fire>» that never misrepresents the schedule
    (Codex #149 R2 M2).

    ``day_delta`` (local_date − utc_date of next_trigger, ∈ {−1,0,1}) shifts
    UTC-framed BYDAY into the user's LOCAL weekday: the runtime advances the
    rule with a UTC dtstart (``rrulestr(dtstart=UTC)``), so BYDAY is UTC and a
    near-midnight reminder would otherwise show the wrong day (Codex R2 M3).
    BYHOUR/BYMINUTE are not used for the clock — that comes from next_trigger.
    """
    parts = _parse_rrule(rrule)
    freq = parts.get("FREQ", "").upper()
    try:
        interval = int(parts.get("INTERVAL", "1"))
    except ValueError:
        interval = 1
    byday = parts.get("BYDAY", "")
    # Bail to the safe fallback on anything we don't render EXACTLY: specific
    # month-days, occurrence caps, ordinal weekdays (1MO), multiple fire-times.
    if parts.get("BYMONTHDAY") or parts.get("COUNT"):
        return None
    if any(ch.isdigit() for ch in byday):  # ordinal BYDAY like 1MO
        return None
    if "," in parts.get("BYHOUR", "") or "," in parts.get("BYMINUTE", ""):
        return None
    # Sub-hour cadences — no weekday, no clock.
    if freq in ("MINUTELY", "HOURLY"):
        if interval > 1:
            return f"каждые {interval} {_plural_ru(interval, _FREQ_UNIT_RU[freq])}"
        return _FREQ_EVERY1_RU[freq]
    # Weekly with explicit days (single interval) → LOCAL weekdays.
    if freq == "WEEKLY" and byday and interval == 1:
        idxs = [_BYDAY_INDEX[d] for d in byday.split(",") if d in _BYDAY_INDEX]
        if not idxs:
            return None
        days = [_WEEKDAY_RU[(i + day_delta) % 7] for i in idxs]
        if len(days) == 1:
            return f"по {days[0]}"
        return "по " + ", ".join(days[:-1]) + f" и {days[-1]}"
    # INTERVAL>1 without a weekday anchor → plain cadence («каждые 2 недели»).
    if interval > 1:
        if byday:  # «каждые 2 недели по пн» is ambiguous → safe fallback
            return None
        if freq in _FREQ_UNIT_RU:
            return f"каждые {interval} {_plural_ru(interval, _FREQ_UNIT_RU[freq])}"
        return None
    # Plain single-interval frequency, no weekday specifics.
    if interval == 1 and not byday and freq in _FREQ_EVERY1_RU:
        return _FREQ_EVERY1_RU[freq]
    return None


def _humanize_until_ru(rrule: str | None, tz: ZoneInfo) -> str:
    """``UNTIL=YYYYMMDD[Thhmmss[Z]]`` → « (до 31 декабря)» in the user's tz."""
    if not rrule:
        return ""
    m = re.search(r"UNTIL=(\d{8})(?:T(\d{6})Z?)?", rrule)
    if not m:
        return ""
    ymd, hms = m.group(1), m.group(2)
    y, mo, d = int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8])
    if hms is None:
        # Date-only UNTIL is a calendar boundary, not an instant — don't shift
        # it across the tz (Codex #149 R2 MINOR: western tz showed prev day).
        return f" (до {d} {_MENU_MONTH_NAMES_RU[mo]})" if 1 <= mo <= 12 else ""
    try:
        dt = datetime(
            y, mo, d, int(hms[0:2]), int(hms[2:4]), int(hms[4:6]),
            tzinfo=timezone.utc,
        ).astimezone(tz)
    except ValueError:
        return ""
    return f" (до {dt.day} {_MENU_MONTH_NAMES_RU[dt.month]})"


def _resolve_user_tz(
    session: Session, tenant_id: str, user_id: str | None
) -> ZoneInfo:
    """User's IANA timezone from profile; Europe/Moscow when no profile
    (system-wide assumption — ``schedule_reminder`` works in MSK). #149."""
    tz_name = "Europe/Moscow"
    if user_id:
        try:
            from sreda.db.repositories.user_profile import UserProfileRepository

            profile = UserProfileRepository(session).get_profile(tenant_id, user_id)
            if profile and profile.timezone:
                tz_name = profile.timezone
        except Exception:  # noqa: BLE001 — display fallback, never block the list
            logger.debug("reminder tz lookup failed; default MSK", exc_info=True)
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Europe/Moscow")


def _format_reminder_for_llm(reminder: Any, tz: ZoneInfo) -> str:
    """LLM-facing reminder line.

    Keeps the ``[rem_…]`` id (the planner reads it from this output to call
    ``update_reminder``) but humanizes the trigger time → ``tz`` and the
    recurrence → Russian. Raw ISO timestamps / RRULE strings used to drop
    the rot's echo below the cyrillic-ratio substitution guards, blanking
    the whole list (#149); humanized text keeps it predominantly cyrillic.
    The rot strips the id for the user per the brain prompt.
    """
    ts = reminder.next_trigger_at
    rrule = reminder.recurrence_rule
    if ts is None:
        when = "время не задано"
    else:
        if ts.tzinfo is None:  # SQLite strips tzinfo on store
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        hhmm = f"{local.hour:02d}:{local.minute:02d}"
        date_ru = f"{local.day} {_MENU_MONTH_NAMES_RU[local.month]}"
        if not rrule:
            when = f"{date_ru} в {hhmm}"
        else:
            # day_delta shifts UTC-framed BYDAY into the user's local weekday.
            day_delta = (local.date() - ts.date()).days
            phrase = _recurrence_phrase(rrule, day_delta)
            until = _humanize_until_ru(rrule, tz)
            freq = _parse_rrule(rrule).get("FREQ", "").upper()
            if phrase is None:
                # Safe: anchor on the REAL next local occurrence — never lies
                # about an RRULE shape we can't render exactly (#149 R2 M2).
                when = f"по расписанию, ближайшее — {date_ru} в {hhmm}{until}"
            elif freq in _SUBHOUR_FREQ:
                when = f"{phrase}{until}"  # «каждые 30 минут» — clock meaningless
            else:
                when = f"{phrase} в {hhmm}{until}"
    return f"[{reminder.id}] {reminder.title} — {when}"


def build_housewife_tools(
    *,
    session: Session,
    tenant_id: str,
    user_id: str | None,
    pending_buttons_state: dict | None = None,
    menu_display_state: dict | None = None,
    embedding_client: Any = None,
    bot_key: str | None = None,
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
    from the returned list.

    ``embedding_client``: optional. When provided, ChecklistService
    embeds new items for recall-broadcast (Phase 2, 2026-05-04).
    None falls back to legacy behaviour (items без embedding,
    backfill подхватит позже).

    ``menu_display_state``: optional mutable dict for user-facing menu
    rendering. ``execute_conversation_chat`` can use it to avoid
    trusting a post-tool LLM rewrite for menu display / creation turns.

    ``bot_key``: the turn's bot_key (from ActionEnvelope.bot_key).
    Passed to schedule_reminder and task_service so new reminders and
    task-linked reminders are stored with the originating bot (Phase 5:
    bot_key fixed at creation).  Falls back to LEGACY_NULL_BOT_KEY when
    not provided (backward-compat).
    """

    service = HousewifeReminderService(session, embedding_client=embedding_client)

    @_write_lc_tool
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
        # R-34 (2026-05-16): WARN log per non-ok return path. Forensic
        # gap из prod incident tg_<X> 2026-05-16 05:44 UTC — bot reply
        # «12:00 прошло», hypothesis = LLM передала вчерашнюю дату,
        # но без логирования tool args не подтверждалось.
        #
        # WARN fields: tenant_id, user_id, trigger_iso_raw,
        # parsed_trigger_utc, server_now_utc, exc_type+exc_str (sanitized)
        # OR late_by_min, title_len. Privacy: no raw title; exception
        # messages truncated и typed чтобы не leak DB params.
        #
        # Codex R1-R4 review fixes:
        # - logger.warning (NOT logger.exception or exc_info=True) —
        #   traceback может содержать SQLAlchemy params (incl. raw title);
        # - exc_type — type name только, guaranteed safe;
        # - exc_str (truncated 200) ТОЛЬКО на bounded paths (parse-error
        #   ValueError из datetime.fromisoformat — known format, и service
        #   ValueError — our raise с controlled message). Generic Exception
        #   + outer fallback: exc_type only (str(SQLAlchemyError) может
        #   embed SQL+bound params);
        # - field names tenant_id/user_id (не tenant/user);
        # - parse-error path: parsed_trigger_utc=None (uniform queries);
        # - tz_label dropped (always "UTC" was misleading — server runs
        #   UTC, user TZ is in profile but not threaded to this scope).
        _server_now_utc = datetime.now(timezone.utc)
        try:
            try:
                trigger_at = datetime.fromisoformat(trigger_iso)
            except ValueError as parse_exc:
                logger.warning(
                    "schedule_reminder error: parse trigger_iso "
                    "tenant_id=%s user_id=%s trigger_iso_raw=%r "
                    "parsed_trigger_utc=None server_now_utc=%s "
                    "exc_type=%s exc_str=%.200s title_len=%d",
                    tenant_id, user_id or "?", trigger_iso,
                    _server_now_utc.isoformat(),
                    type(parse_exc).__name__, str(parse_exc),
                    len(title or ""),
                )
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
                    logger.warning(
                        "schedule_reminder skipped: past trigger "
                        "tenant_id=%s user_id=%s trigger_iso_raw=%r "
                        "parsed_trigger_utc=%s server_now_utc=%s "
                        "late_by_min=%d title_len=%d",
                        tenant_id, user_id or "?", trigger_iso,
                        trigger_at.isoformat(), now_utc.isoformat(),
                        int(late_by_min), len(title or ""),
                    )
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
                    bot_key=bot_key,
                )
            except ValueError as exc:
                logger.warning(
                    "schedule_reminder error: service ValueError "
                    "tenant_id=%s user_id=%s trigger_iso_raw=%r "
                    "parsed_trigger_utc=%s server_now_utc=%s "
                    "exc_type=%s exc_str=%.200s title_len=%d",
                    tenant_id, user_id or "?", trigger_iso,
                    trigger_at.isoformat(), _server_now_utc.isoformat(),
                    type(exc).__name__, str(exc), len(title or ""),
                )
                return f"error: {exc}"
            except Exception as exc:  # noqa: BLE001
                # Codex R3 MAJOR privacy fix: drop exc_str entirely from
                # generic Exception paths. str(SQLAlchemyError) может
                # содержать SQL statement + bound parameter values
                # (включая raw title). 200-char truncation не guarantee
                # safety. Только exc_type — type name — гарантированно
                # safe. Diagnostic loss accepted; deep-dive = local repro.
                logger.warning(
                    "schedule_reminder failed: generic exception "
                    "tenant_id=%s user_id=%s trigger_iso_raw=%r "
                    "parsed_trigger_utc=%s server_now_utc=%s "
                    "exc_type=%s title_len=%d",
                    tenant_id, user_id or "?", trigger_iso,
                    trigger_at.isoformat(), _server_now_utc.isoformat(),
                    type(exc).__name__, len(title or ""),
                )
                return "error: internal"
        except Exception as exc:  # noqa: BLE001 — outer fallback
            # Same privacy treatment as inner generic: exc_type only,
            # no exc_str.
            logger.warning(
                "schedule_reminder unexpected outer exception "
                "tenant_id=%s user_id=%s trigger_iso_raw=%r "
                "parsed_trigger_utc=None server_now_utc=%s "
                "exc_type=%s title_len=%d",
                tenant_id, user_id or "?", trigger_iso,
                _server_now_utc.isoformat(),
                type(exc).__name__, len(title or ""),
            )
            return "error: internal"

        return f"ok:scheduled:{reminder.id}:{reminder.next_trigger_at.isoformat()}"

    @lc_tool
    def list_reminders(title_match: str | None = None) -> str:
        """List all pending reminders for the current user.

        Args:
            title_match: подстрока названия (регистр/ё неважны) — для
                «отмени напоминание про X»: вернутся только совпадающие,
                дальше бери ровно-одно через ``.only``.

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

        if title_match:  # #122: выбор по имени
            reminders = [
                r for r in reminders if _title_matches(title_match, r.title)
            ]
        if not reminders:
            return "no active reminders"
        _tz = _resolve_user_tz(session, tenant_id, user_id)
        lines = [_format_reminder_for_llm(r, _tz) for r in reminders[:20]]
        return "active reminders:\n" + "\n".join(lines)

    @_write_lc_tool
    def update_reminder(
        reminder_id: str,
        *,
        title: str | None = None,
        trigger_iso: str | None = None,
        recurrence_rule: str | None = None,
        clear_recurrence: bool = False,
    ) -> str:
        """Update a pending reminder IN-PLACE — preferred over
        cancel + schedule when the user is refining an existing reminder.

        WHEN TO USE:
        - Юзер уточняет существующее: «увеличь до 20:00», «не каждый
          час, а каждые 30 мин», «timezone +1», «измени на пятницу».
        - Любое уточнение, которое правит **тот же** reminder.

        WHEN NOT TO USE:
        - Юзер просит **новый** reminder (отдельная сущность) →
          ``schedule_reminder``.
        - Юзер хочет **отменить** существующий → ``cancel_reminder``.

        FLOW:
        1. Сначала ``list_reminders`` чтобы получить ``reminder_id``.
        2. Потом ``update_reminder(rem_xxx, **fields_to_change)`` —
           передавай ТОЛЬКО изменяющиеся поля. Неуказанные остаются как есть.

        Args:
            reminder_id: id reminder'а (`rem_xxx`) — взять из
                ``list_reminders``.
            title: Новый текст (если меняется).
            trigger_iso: Новое время в ISO-8601. Те же правила что у
                schedule_reminder (см. его docstring) — UTC, либо ISO с
                offset'ом, который сконвертится в UTC.
            recurrence_rule: Новая RFC-5545 RRULE string (BYHOUR/BYMINUTE
                в UTC). Передавай для смены частоты.
            clear_recurrence: Если True — убрать recurrence (превратить в
                one-shot). Используй когда юзер просит «убери повтор».

        Returns:
            ``ok:updated:rem_xxx:<next_trigger_at_iso>`` либо
            ``error: not found`` либо ``error: <reason>``.
        """
        clean_id = (reminder_id or "").strip()
        if not clean_id:
            return "error: reminder_id required"

        # Parse trigger_iso если задан — те же правила что в schedule_reminder.
        trigger_at_dt: datetime | None = None
        if trigger_iso is not None:
            try:
                trigger_at_dt = datetime.fromisoformat(trigger_iso)
            except ValueError:
                return f"error: cannot parse trigger_iso={trigger_iso!r}"
            if trigger_at_dt.tzinfo is None:
                trigger_at_dt = trigger_at_dt.replace(tzinfo=timezone.utc)
            else:
                trigger_at_dt = trigger_at_dt.astimezone(timezone.utc)

        try:
            rem = service.update(
                tenant_id=tenant_id,
                reminder_id=clean_id,
                title=title,
                trigger_at=trigger_at_dt,
                recurrence_rule=recurrence_rule,
                clear_recurrence=clear_recurrence,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("update_reminder failed")
            return "error: internal"

        if rem is None:
            return f"error: reminder {clean_id!r} not found"
        next_at = rem.next_trigger_at.isoformat() if rem.next_trigger_at else "none"
        return f"ok:updated:{rem.id}:{next_at}"

    @_write_lc_tool
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

    @_write_lc_tool
    def onboarding_answered(topic: str, summary: str) -> str:
        """Mark the addressing onboarding topic as answered.

        Codex Sub-A4 onboarding R4 MAJOR (prompt-surface): pre-R4 the
        docstring advertised all 6 legacy topics, but TOPIC_ORDER
        (housewife_onboarding.py:68) was reduced to only `addressing`
        in 2026-04-27. Other 5 topics are valid IDs in TOPIC_DESCRIPTIONS
        but not in active flow.

        Call this when the user has given you a meaningful answer to the
        addressing prompt (see [ОНБОРДИНГ] in the system prompt). The
        ``summary`` is what will be stored — a 1–2 sentence paraphrase
        in the user's own words.

        Args:
            topic: Must be `addressing` (only active topic). Match the
                [ОНБОРДИНГ] block's current_topic.
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
        # Codex Sub-A4 onboarding R5 MAJOR (HIGH catch): TOPIC_DESCRIPTIONS
        # keeps all 6 legacy topics for backward compat, but TOPIC_ORDER
        # (the ACTIVE flow) is just `addressing` since 2026-04-27. Reject
        # inactive topics here so legacy callers can't persist hidden
        # rows in skill_params_json that no progression engine counts.
        if topic_norm not in TOPIC_ORDER:
            return f"error: topic_not_in_active_flow {topic!r}"
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

    @_write_lc_tool
    def onboarding_deferred(topic: str, reason: str) -> str:
        """Mark the addressing onboarding topic as deferred.

        Codex Sub-A4 onboarding R4 MAJOR (prompt-surface): pre-R4
        docstring listed all 6 legacy topics and called reason
        «for audit». Active flow is only `addressing` per TOPIC_ORDER
        (housewife_onboarding.py:68). Reason is NOT stored — kept as
        planner-facing self-reflection field.

        Call this when the user explicitly wants to skip the current
        topic ("потом", "не сейчас", "пропусти"). First skip keeps
        the topic in a retry queue; second skip on the same topic
        makes the skip permanent.

        Args:
            topic: Must be `addressing` (only active topic).
            reason: Short rationale for skipping — runtime does NOT
                store this value; planner is asked to articulate
                its skip decision in plan_json for transparency.

        Returns short status string.
        """
        topic_norm = (topic or "").strip().lower()
        if topic_norm not in TOPIC_DESCRIPTIONS:
            return f"error: unknown topic {topic!r}"
        # Codex Sub-A4 onboarding R5 MAJOR (HIGH catch): see same
        # guard in onboarding_answered above. Reject inactive topics
        # so legacy callers can't write hidden rows.
        if topic_norm not in TOPIC_ORDER:
            return f"error: topic_not_in_active_flow {topic!r}"
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

    @_write_lc_tool
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

    @_write_lc_tool
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

        Returns a status envelope with the added item NAMES (plus names of
        items that were already on the list / repeated in the batch) and the
        new row ids (needed if the user immediately changes their mind and
        you need to remove_shopping_items the last add).
        """
        if not user_id:
            return "error: no user_id context"
        if not items:
            return "error: empty items list"
        try:
            outcome = shopping_service.add_items_detailed(
                tenant_id=tenant_id, user_id=user_id, items=items
            )
        except Exception:  # noqa: BLE001
            logger.exception("add_shopping_items failed")
            return "error: internal"
        # #115: okv2 carries by-name outcome buckets. Status routing:
        #   created>0            → added   (the honest count — duplicates no
        #                          longer inflate it on the planner path)
        #   created=0, replay>0  → replay  (same-operation retry; idempotent)
        #   otherwise            → empty   (all-duplicate / nothing insertable)
        dup_fields = {
            "duplicates_existing": list(outcome.duplicates_existing),
            "duplicates_in_batch": list(outcome.duplicates_in_batch),
            "duplicate_item_ids": list(outcome.duplicate_item_ids),
        }
        if outcome.created:
            return encode_tool_ok(
                "added",
                {
                    "added_count": len(outcome.created),
                    "item_ids": [r.id for r in outcome.created],
                    "created": [r.title for r in outcome.created],
                    "replayed": [r.title for r in outcome.replayed],
                    "invalid": [],
                    **dup_fields,
                },
            )
        if outcome.replayed:
            return encode_tool_ok(
                "replay",
                {
                    "replayed": [r.title for r in outcome.replayed],
                    "item_ids": [r.id for r in outcome.replayed],
                    **dup_fields,
                },
            )
        if outcome.duplicates_existing or outcome.duplicates_in_batch:
            return encode_tool_ok("empty", {"added_count": 0, **dup_fields})
        # Nothing insertable and nothing nameable (e.g. all titles blank).
        return "ok:added:0"

    @_write_lc_tool
    def mark_shopping_bought(item_ids: list[str]) -> str:
        """Mark shopping list items as bought (checked off).

        Call when the user says they've bought something. Items move to
        ``bought`` status and disappear from the normal view. Batch —
        one call for all items the user mentioned.

        Args:
            item_ids: List of ids (each starts with ``sh_``) to mark as
                bought. Get the ids from the most recent ``list_shopping``
                output or from an earlier ``add_shopping_items`` return.

        Returns a ``bought`` envelope with the affected item NAMES, the
        names of owned-but-ineligible rows, and a count of unknown ids.
        """
        if not user_id:
            return "error: no user_id context"
        if not item_ids:
            return "error: empty item_ids"
        try:
            outcome = shopping_service.mark_bought_detailed(
                tenant_id=tenant_id, user_id=user_id, ids=item_ids
            )
        except Exception:  # noqa: BLE001
            logger.exception("mark_shopping_bought failed")
            return "error: internal"
        # #115: okv2 carries the affected NAMES (+ skipped/unknown breakdown).
        return encode_tool_ok(
            "bought",
            {
                "bought_count": len(outcome.affected),
                "marked": [r.title for r in outcome.affected],
                "not_eligible": list(outcome.not_eligible),
                "not_found_count": outcome.not_found_count,
            },
        )

    @_write_lc_tool
    def remove_shopping_items(item_ids: list[str]) -> str:
        """Remove items from the shopping list (cancel without buying).

        Use when the user says "убери молоко из списка" / "не надо
        хлеб" / "перехотел". Different from mark_shopping_bought —
        these items were never bought. Both move out of the user's view.

        Args:
            item_ids: List of ids to cancel.

        Returns a ``removed`` envelope with the affected item NAMES, the
        names of owned-but-ineligible rows, and a count of unknown ids.
        """
        if not user_id:
            return "error: no user_id context"
        if not item_ids:
            return "error: empty item_ids"
        try:
            outcome = shopping_service.remove_items_detailed(
                tenant_id=tenant_id, user_id=user_id, ids=item_ids
            )
        except Exception:  # noqa: BLE001
            logger.exception("remove_shopping_items failed")
            return "error: internal"
        # #115: okv2 carries the affected NAMES (+ skipped/unknown breakdown).
        return encode_tool_ok(
            "removed",
            {
                "removed_count": len(outcome.affected),
                "removed": [r.title for r in outcome.affected],
                "not_eligible": list(outcome.not_eligible),
                "not_found_count": outcome.not_found_count,
            },
        )

    @_write_lc_tool
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

        Returns an ``updated`` envelope with the item name before/after
        the update, or an error message if the item is not found.
        """
        if not user_id:
            return "error: no user_id context"
        if not item_id or not item_id.strip():
            return "error: empty item_id"
        try:
            detailed = shopping_service.update_item_detailed(
                tenant_id=tenant_id, user_id=user_id,
                item_id=item_id.strip(),
                title=title, quantity_text=quantity_text,
                category=category,
            )
        except Exception:  # noqa: BLE001
            logger.exception("update_shopping_item failed")
            return "error: internal"
        if detailed is None:
            return f"error: item {item_id!r} not found"
        row, previous_title = detailed
        # #115: okv2 carries the item name before/after (voice can say «X → Y»).
        return encode_tool_ok(
            "updated",
            {"item_id": row.id, "previous_name": previous_title, "new_name": row.title},
        )

    @_write_lc_tool
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

        Returns an ``updated_category`` envelope with the affected item
        NAMES, the target category, and a count of unknown/foreign ids.
        """
        if not user_id:
            return "error: no user_id context"
        if not item_ids:
            return "error: empty item_ids"
        if not category or not category.strip():
            return "error: empty category"
        try:
            outcome, stored_category = shopping_service.update_items_category_detailed(
                tenant_id=tenant_id, user_id=user_id,
                ids=item_ids, category=category,
            )
        except Exception:  # noqa: BLE001
            logger.exception("update_shopping_items_category failed")
            return "error: internal"
        # #115: okv2 carries the affected NAMES + the category ACTUALLY stored
        # (coerced), so the voice never advertises a bucket the DB doesn't have.
        return encode_tool_ok(
            "updated_category",
            {
                "updated_count": len(outcome.affected),
                "updated": [r.title for r in outcome.affected],
                "not_eligible": list(outcome.not_eligible),
                "category": stored_category,
                "not_found_count": outcome.not_found_count,
            },
        )

    @lc_tool
    def list_shopping(title_match: str | None = None) -> str:
        """List the user's pending (not-yet-bought, not-cancelled)
        shopping items.

        Args:
            title_match: подстрока названия (регистр/ё неважны) — фильтр
                для «удали/отметь X по имени»: вернутся только совпадающие
                пункты, дальше бери ровно-один через ``.only``.

        Returns a compact text dump grouped by category with ids so
        subsequent mark_shopping_bought / remove_shopping_items can
        reference rows. Use when the user asks "что в списке?",
        "что покупать?", "сколько осталось?".

        Empty list (или ничего не совпало под title_match) returns the
        exact string ``no shopping items``.
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
        if title_match:  # #122: выбор по имени
            rows = [r for r in rows if _title_matches(title_match, r.title)]
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

    @_write_lc_tool
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

    @_write_lc_tool
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
        already exists, this signals status ``duplicate`` WITHOUT
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

        Returns a status envelope carrying the recipe name and id.
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
        # #115: emit okv2 envelope carrying the recipe NAME so the live voice
        # can confirm by name (was id-only `ok:saved:<id>` — Sub-A4 drop).
        if not is_new:
            # Title already existed for this user — tell the LLM so it
            # can surface "рецепт уже был в книге" to the user instead
            # of claiming it just created one.
            return encode_tool_ok("duplicate", {"recipe_id": recipe.id, "title": recipe.title})
        return encode_tool_ok("saved", {"recipe_id": recipe.id, "title": recipe.title})

    @_write_lc_tool
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

        Returns a ``batch_saved`` status envelope carrying the affected recipe
        NAMES grouped as created / already-existing / repeated-in-batch / invalid
        (plus created_count + ids for branching). (Legacy positional
        ``ok:batch_saved:...`` is still parsed for historical replay.)
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
        # #115: okv2 envelope carrying by-name outcome buckets so the live voice
        # can confirm what was saved / was already there / repeated / failed.
        return encode_tool_ok(
            "batch_saved",
            {
                "created_count": len(outcome.created),
                "skipped_as_duplicate": len(outcome.skipped_existing),
                "recipe_ids": [r.id for r in outcome.created],
                "created": [r.title for r in outcome.created],
                "duplicates_existing": [r.title for r in outcome.skipped_existing],
                "duplicates_in_batch": list(outcome.duplicates_in_batch),
                "invalid": list(outcome.invalid),
            },
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

    @_write_lc_tool
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

    @_write_lc_tool
    def plan_week_menu(
        week_start: str,
        days: list[dict[str, Any]],
        notes: str | None = None,
    ) -> str:
        """Create (or update) the weekly menu for the user.

        ⚠️ **PRESERVE-MERGE.** Если для week_start уже есть план,
        переданные ячейки ПЕРЕЗАПИШУТ соответствующие слоты, а ячейки,
        которые ты НЕ передал, ОСТАНУТСЯ как были. Чтобы стереть один
        слот — передай его с пустыми полями (recipe_id=None +
        free_text=None — это явный clear). Чтобы стереть всю неделю
        и собрать заново — сначала ``clear_menu``, потом
        ``plan_week_menu``. Для точечной правки ОДНОЙ ячейки
        используй ``update_menu_item``.

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
        if menu_display_state is not None:
            menu_display_state["plan_week_menu_calls"] = (
                int(menu_display_state.get("plan_week_menu_calls") or 0) + 1
            )
            menu_display_state["last_planned_menu_reply_text"] = (
                _format_menu_plan_for_user(plan)
            )
        return f"ok:plan_created:{plan.id}:{plan.week_start_date.isoformat()}"

    @_write_lc_tool
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

        Returns an LLM-readable weekly menu grouped by dated day blocks.
        Recipe links are still shown as [rec_...] ids so get_recipe can
        follow them, but the layout is intentionally close to the
        user-facing shape to avoid the model gluing days together.
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

        if menu_display_state is not None:
            menu_display_state["list_menu_calls"] = (
                int(menu_display_state.get("list_menu_calls") or 0) + 1
            )
            menu_display_state["last_menu_reply_text"] = _format_menu_plan_for_user(
                plan
            )

        lines = [
            f"menu_id: {plan.id}",
            f"week_start: {plan.week_start_date.isoformat()}",
            "",
        ]
        lines.extend(
            _iter_menu_plan_day_lines(
                plan,
                include_recipe_ids=True,
                bullet_meals=False,
            )
        )
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

    @_write_lc_tool
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

        Returns one of:
          - ``ok:generated:N:eaters=E`` — N items added, E = family size
          - ``ok:generated:0:eaters=E`` — plan exists with recipes, but
            ingredient→shopping conversion dropped everything (e.g. all
            «по вкусу»). Eaters is known.
          - ``ok:plan_no_recipes`` — plan exists but every cell is
            free_text (no recipe_id) — nothing to extract. Distinct
            from «empty conversion» because the planner can tell the
            user «у этого меню нет сохранённых рецептов, я не могу
            собрать список автоматически».
          - ``error:plan_not_found`` — plan_id is unknown or doesn't
            belong to this user. Distinct from «empty» so the planner
            can tell the user «не нашла такого меню» rather than
            «покупок нет».

        Codex Sub-A4 menu R3 MAJOR (gen_shopping split): R2 reviewer
        flagged that unknown plan_id and free_text-only plan both
        mapped to ``ok:generated:0``, indistinguishable. Split: unknown
        plan_id → ``error:plan_not_found``; recipe-less plan →
        ``ok:plan_no_recipes``; empty conversion → ``ok:generated:0:eaters=E``;
        normal → ``ok:generated:N:eaters=E``.
        """
        if not user_id:
            return "error: no user_id context"
        # R3 fix: probe plan existence BEFORE aggregating so we can
        # distinguish unknown plan_id from empty plan downstream.
        plan = menu_service._get_plan(
            tenant_id=tenant_id, user_id=user_id, plan_id=plan_id.strip()
        )
        if plan is None:
            return "error:plan_not_found"
        # R3 fix: distinguish «no recipe_id cells» from «empty conversion».
        # If the plan has zero recipe_id'd items, aggregation would
        # return [] anyway — the planner needs to know WHY.
        has_recipe_cells = any(
            item.recipe_id is not None for item in plan.items
        )
        if not has_recipe_cells:
            return "ok:plan_no_recipes"
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
            # Recipe cells exist but produced no ingredients — recipe
            # has empty ingredients list. Carry eaters anyway so the
            # composer can render «масштабировала на N человек, но
            # рецепты без ингредиентов» honestly.
            return f"ok:generated:0:eaters={eaters}"

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

    @_write_lc_tool
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
            outcome = family_service.add_members_batch_detailed(
                tenant_id=tenant_id, user_id=user_id, members=members
            )
        except Exception:  # noqa: BLE001
            logger.exception("add_family_members failed")
            return "error: internal"
        # #115: keep `skipped_as_duplicate` as the legacy aggregate count
        # (members − created) so the existing validator invariant holds; the
        # by-name buckets below carry the precise breakdown for the live voice.
        created = outcome.created
        skipped = max(0, len(members) - len(created))
        return encode_tool_ok(
            "added",
            {
                "added_count": len(created),
                "skipped_as_duplicate": skipped,
                "member_ids": [m.id for m in created],
                "created": [m.name for m in created],
                "duplicates_existing": list(outcome.duplicates_existing),
                "duplicates_in_batch": list(outcome.duplicates_in_batch),
                "invalid": list(outcome.invalid),
            },
        )

    @lc_tool
    def list_family_members() -> str:
        """List all recorded household members for the current user.

        Use cases:
          - User asks «кто у меня в семье»
          - You need ``member_id`` to call ``update_family_member`` /
            ``remove_family_member`` and don't have it yet

        Note: ``plan_week_menu`` and ``generate_shopping_from_menu``
        DO NOT require this call — both pull household via
        ``family_service.count_eaters`` internally. Don't add a
        redundant ``list_family_members`` step before them.

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

    @_write_lc_tool
    def update_family_member(
        member_id: str,
        name: str | None = None,
        role: str | None = None,
        birth_year: int | None = None,
        clear_birth_year: bool = False,
        age_hint: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Update fields of an existing family member.

        Pass None / leave out args you don't want to change. Use when
        user corrects info: "Маше 9 уже" → set birth_year or age_hint;
        "у Никиты аллергия на молоко теперь" → set notes.

        Codex Sub-A4 household R3 MAJOR: pass ``clear_birth_year=True``
        to null the birth_year column (e.g. «убери год рождения»).
        Mutually exclusive with ``birth_year=N`` — passing both
        returns ``error: birth_year and clear_birth_year are mutually
        exclusive ...``. age_hint / notes are cleared by passing an
        empty string ``""``.

        Args:
            member_id: id from list_family_members (``fm_...``).
            name / role / birth_year / age_hint / notes: new values.
                Roles: self | spouse | child | parent | other.
            clear_birth_year: when True, nulls birth_year column.

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
                clear_birth_year=clear_birth_year,
                age_hint=age_hint,
                notes=notes,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("update_family_member failed")
            return "error: internal"
        # #115: okv2 carries the updated member NAME for the live voice; not-found
        # stays the existing error path (no name to show — functionally "not_found").
        if row is None:
            return f"error: member {member_id!r} not found"
        return encode_tool_ok("updated", {"name": row.name})

    @_write_lc_tool
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

    task_service = TaskService(session, reminder_service=service, bot_key=bot_key)

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

    @_write_lc_tool
    def add_task(
        title: str,
        scheduled_date: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        recurrence_rule: str | None = None,
        notes: str | None = None,
        reminder_offset_minutes: int | None = None,
        details_items: list[str] | None = None,
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
            details_items: R-33 (2026-05-15) — optional list of
                "component sub-items" чтобы хранить detals В ОТДЕЛЬНОМ
                ЧЕКЛИСТЕ вместо длинного title. Если передаёшь — создаётся
                fresh Checklist с тем же title, items добавляются туда,
                task получает link на этот checklist. В UI расписания
                показывается короткий title + counter «N/M» + раскрывается
                inline (accordion). **PRINCIPLE**: details_items для
                задач-«контейнеров» где SAME verb + список ОБЪЕКТОВ.
                Если несколько РАЗНЫХ verbs — это РАЗНЫЕ tasks → multiple
                add_task calls, не details_items.

                **КЛЮЧЕВОЕ ПРАВИЛО (Boris 2026-05-15)**:
                - Same verb + перечисление объектов → 1 task с
                  details_items. «Купи хлеб, макароны, яйца» →
                  title="Купить", details_items=["хлеб","макароны","яйца"].
                - Разные verbs → multiple add_task calls. «Купи хлеб,
                  помой машину, заедь в банк» → 3 separate add_task
                  calls (не details_items!).

                POSITIVE EXAMPLES (use details_items):
                - User: «Купи хлеб, макароны и яйца завтра» →
                  title="Купить", details_items=["хлеб","макароны","яйца"]
                  (same verb «купить» + 3 объекта).
                - User: «Поставь крой на пятницу: страйп белый, ледяная
                  мята, жемчуг» → title="Крой", scheduled_date="2026-05-15",
                  details_items=["страйп белый","ледяная мята","жемчуг"]
                  (1 cutting session + перечисление тканей).
                - User: «Сделай задачу уборка на завтра: пропылесосить,
                  мыть пол, окна» → title="Уборка",
                  details_items=["пропылесосить","мыть пол","окна"]
                  (1 cleaning activity + sub-steps).
                - User: «На субботу поехать в храм: почитать молитву,
                  собрать вещи, включить машину» → title="Поехать в храм",
                  scheduled_date="2026-05-16",
                  details_items=["почитать молитву","собрать вещи",
                                 "включить машину"]
                  (1 trip + подготовительные steps).

                NEGATIVE EXAMPLES (do NOT use details_items):
                - User: «Купи молоко завтра» → title="Купить молоко",
                  details_items=None (один item — НЕ нужен checklist).
                - User: «Купи хлеб, помой машину, заедь в банк» →
                  **3 SEPARATE add_task calls** (разные verbs!):
                    1. add_task(title="Купить хлеб", ...)
                    2. add_task(title="Помыть машину", ...)
                    3. add_task(title="Заехать в банк", ...)
                  НЕ объединять в один details_items.
                - User: «Позвони врачу в 14:00» → title="Позвонить врачу",
                  details_items=None.
                - User: «Напомни про лекарство утром» →
                  title="Принять лекарство", details_items=None.

        Returns ``ok:created:task_<id>`` (possibly с ``+checklist`` suffix
        if details_items привёл к чеклисту) or ``error:<reason>``.
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

        # R-33 R1 MAJOR (Codex fix): reminder + details_items not supported
        # together. add_task_with_details composite path doesn't call
        # _attach_reminder_inner — silent drop would be misleading.
        # Reject explicitly so LLM uses correct pattern:
        # 1) add_task с details_items → task + checklist
        # 2) attach_reminder(task_id, offset_minutes) separately
        if details_items and reminder_offset_minutes is not None:
            return (
                "error: reminder_offset_minutes не поддерживается вместе "
                "с details_items. Сначала создай task с details_items, "
                "потом attach_reminder(task_id, offset_minutes) отдельным "
                "tool_call'ом."
            )

        # R-33: composite path (details_items != None) needs fresh session
        # ownership (Codex R4: caller manages TX). Tool opens own session;
        # default closure session uses commit-per-method pattern (legacy
        # add_task), composite path uses fresh session + caller commit.
        if details_items:
            # Composite create через add_task_with_details (atomic TX).
            from sreda.db.session import get_session_factory
            from sreda.services.tasks import TaskService as _TS
            sf = get_session_factory()
            with sf() as fresh_session:
                try:
                    svc = _TS(fresh_session, bot_key=bot_key)
                    task, created_details = svc.add_task_with_details(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        title=title,
                        scheduled_date=date_obj,
                        time_start=t_start,
                        time_end=t_end,
                        recurrence_rule=recurrence_rule,
                        notes=notes,
                        reminder_offset_minutes=reminder_offset_minutes,
                        details_items=details_items,
                    )
                    fresh_session.commit()
                    # #115: okv2 carries the task name + the ACTUALLY-created
                    # checklist item names (within-batch dedup already applied —
                    # no over-reporting of duplicate inputs).
                    if task.checklist_id:
                        return encode_tool_ok(
                            "created_with_checklist",
                            {
                                "task_id": task.id,
                                "checklist_id": task.checklist_id,
                                "task_title": task.title,
                                "details_added": created_details,
                            },
                        )
                    return encode_tool_ok(
                        "created", {"task_id": task.id, "task_title": task.title}
                    )
                except ValueError as exc:
                    fresh_session.rollback()
                    return f"error:{exc}"
                except Exception:  # noqa: BLE001
                    fresh_session.rollback()
                    logger.exception("add_task_with_details failed")
                    return "error: internal"

        # Legacy path (no details) — existing closure session + commit-per-method
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
        # #115: okv2 carries the task name (+ reminder offset on the reminder path).
        if task.reminder_id:
            return encode_tool_ok(
                "created_with_reminder",
                {
                    "task_id": task.id,
                    "reminder_offset_minutes": task.reminder_offset_minutes,
                    "task_title": task.title,
                },
            )
        return encode_tool_ok("created", {"task_id": task.id, "task_title": task.title})

    @lc_tool
    def list_tasks(date: str = "today", status: str = "pending",
                   title_match: str | None = None) -> str:
        """List tasks filtered by date and status.

        Args:
            date: ``"today"`` (default) / ``"tomorrow"`` / ISO date /
                ``"inbox"`` (tasks without a date) / ``"all"`` (no
                date filter).
            title_match: подстрока названия (регистр/ё неважны) — для
                «выполнена/отмени/измени задачу X»: используй date="all"
                + title_match, дальше ``.only``.
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
        if title_match:  # #122: выбор по имени
            rows = [t for t in rows if _title_matches(title_match, t.title)]
        if not rows:
            return "no tasks"
        return "\n".join(_fmt_task_for_llm(t) for t in rows)

    @_write_lc_tool
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
        return encode_tool_ok("updated", {"task_id": task.id, "title": task.title})  # #115

    @_write_lc_tool
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
        return encode_tool_ok("completed", {"task_id": task.id, "title": task.title})  # #115

    @_write_lc_tool
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
        return encode_tool_ok("uncompleted", {"task_id": task.id, "title": task.title})  # #115

    @_write_lc_tool
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
        return encode_tool_ok("cancelled", {"task_id": task.id, "title": task.title})  # #115

    @_write_lc_tool
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

    @_write_lc_tool
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

    @_write_lc_tool
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

    @_write_lc_tool
    def link_task_to_checklist(task_id: str, checklist_id: str) -> str:
        """R-33 (2026-05-15): explicit 1-to-1 link existing task → existing checklist.

        Use when user wants to **attach** an existing detail-checklist to an
        existing task. Например, юзер ранее создал task «Уборка» и отдельно
        checklist «Уборка items» — теперь хочет их связать чтобы расписание
        показало 📋 button.

        WHEN TO USE:
        - Both task и checklist уже существуют (созданы отдельно)
        - User asks «связать задачу X со списком Y», «прикрепи список к
          задаче»

        WHEN NOT TO USE:
        - Creating new task with details → use ``add_task(details_items=...)``
          instead — composite create в одной transaction
        - Detach link → use ``unlink_task``

        Args:
            task_id: id task'а (``task_xxx``), от ``list_tasks``
            checklist_id: id checklist'а (``checklist_xxx``), от
                ``list_checklists``

        Returns:
            ``ok:linked:task_xxx:checklist_yyy`` — newly linked
            ``ok:already_linked:task_xxx:checklist_yyy`` — same pair existed
            ``error: not found`` — task/checklist missing or wrong owner
            ``error: archived`` — checklist not active
            ``error: task_already_linked:task_xxx:checklist_yyy`` — task
                имеет other checklist, unlink сначала через unlink_task
            ``error: checklist_already_linked_to_task_xxx`` — checklist
                принадлежит другой task'е
        """
        if not user_id:
            return "error: no user_id context"
        from sreda.db.session import get_session_factory
        from sreda.services.tasks import TaskService as _TS
        sf = get_session_factory()
        with sf() as fresh_session:
            try:
                svc = _TS(fresh_session)
                status, info = svc.link_to_checklist(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    task_id=task_id.strip(),
                    checklist_id=checklist_id.strip(),
                )
                if status.startswith("ok"):
                    fresh_session.commit()
                    return f"{status}:{task_id.strip()}:{checklist_id.strip()}"
                fresh_session.rollback()
                if status == "error:task_already_linked_to_other":
                    return (
                        f"error: task_already_linked:{task_id.strip()}:"
                        f"{info}. Unlink сначала через unlink_task."
                    )
                if status == "error:checklist_already_linked_to_other":
                    return (
                        f"error: checklist_already_linked_to_task_{info}. "
                        "Сначала unlink другую задачу через unlink_task."
                    )
                return f"{status}: {info or ''}"
            except Exception:  # noqa: BLE001
                fresh_session.rollback()
                logger.exception("link_task_to_checklist failed")
                return "error: internal"

    @_write_lc_tool
    def unlink_task(task_id: str) -> str:
        """R-33 (2026-05-15): unlink task from its checklist (если linked).

        Use when user wants to **detach** detail-checklist from task. Task
        и checklist оба остаются (separate entities) — только break the link.

        Args:
            task_id: id task'а (``task_xxx``)

        Returns:
            ``ok:unlinked:task_xxx:checklist_yyy`` — was linked, now unlinked
            ``ok:not_linked:task_xxx`` — wasn't linked (idempotent)
            ``error: not found`` — task missing or wrong owner
        """
        if not user_id:
            return "error: no user_id context"
        from sreda.db.session import get_session_factory
        from sreda.services.tasks import TaskService as _TS
        sf = get_session_factory()
        with sf() as fresh_session:
            try:
                svc = _TS(fresh_session)
                status, info = svc.unlink_from_checklist(
                    tenant_id=tenant_id, user_id=user_id, task_id=task_id.strip(),
                )
                if status == "ok:unlinked":
                    fresh_session.commit()
                    return f"ok:unlinked:{task_id.strip()}:{info}"
                if status == "ok:not_linked":
                    fresh_session.commit()
                    return f"ok:not_linked:{task_id.strip()}"
                fresh_session.rollback()
                return f"{status}: {info or ''}"
            except Exception:  # noqa: BLE001
                fresh_session.rollback()
                logger.exception("unlink_task failed")
                return "error: internal"

    @_write_lc_tool
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
    checklist_service = ChecklistService(session, embedding_client=embedding_client)

    @_write_lc_tool
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

    @_write_lc_tool
    def add_checklist_items(list_id_or_title: str, items: list[str]) -> str:
        """Добавить пункты в чек-лист (создаёт новый если такого нет).

        Триггеры пользователя (вызывай этот tool сразу, БЕЗ create_checklist):
        - «запиши в дела по машине: колодки, стекло, масло»
        - «добавь в дела по даче: привезти лопату, купить рассаду»
        - «зафиксируй в список покупок для отпуска: палатка, спрей от комаров»
        - «запиши план кроя на эту неделю: лаванда 298 простыня 141×200, шампань 202×204»
        - «добавь в дела по детям: репетитор математика, врач стоматолог»
        - «запиши в материалы для ремонта: краска белая 5л, валик, малярный скотч»

        Tool САМ создаёт чек-лист с таким title если его ещё нет — НЕ
        нужно делать отдельный create_checklist + add_checklist_items.
        Это ОДИН вызов на оба действия (create + populate). create_checklist
        используй ТОЛЬКО когда юзер просит пустой список без items
        («заведи новый список Дача без пунктов»).

        Args:
            list_id_or_title: id вида ``checklist_*`` ИЛИ короткое
                название (нечёткий поиск среди активных списков, например
                «План кроя», «Дела по машине», «Дела на дачу»). При
                отсутствии — создаст новый список с этим title.
            items: список пунктов как строки, по одному на строку, например:
                ["Лаванда 298 ТС, простыня 141×200×19",
                 "Шампань страйп, простыня 202×204×26"]
                или
                ["Заменить колодки (скрипит)",
                 "Проверить водительское стекло",
                 "Починить пассажирское стекло"].

        Returns: ``ok:added:N:list=<id>`` или ``error:<msg>``.
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
        # #115: okv2 carries the added item NAMES (+ skipped duplicate names) so
        # the live voice can say «добавила: X, Y; уже было: Z» (was count-only).
        if skipped_dup:
            return encode_tool_ok(
                "added_with_dups",
                {
                    "added_count": len(added),
                    "duplicate_count": len(skipped_dup),
                    "checklist_id": cl.id,
                    "created": [i.title for i in added],
                    "duplicates_existing": list(skipped_dup),
                },
            )
        return encode_tool_ok(
            "added",
            {
                "added_count": len(added),
                "duplicate_count": 0,
                "checklist_id": cl.id,
                "created": [i.title for i in added],
            },
        )

    @_write_lc_tool
    def move_task_to_checklist(
        task_id: str, list_id_or_title: str
    ) -> str:
        """Move a task from schedule to a checklist (best-effort staged writes).

        Codex Sub-A4 checklists R2 MAJOR (prior-not-closed): pre-R2
        docstring claimed «atomically», «one transaction», «cannot
        lose partially». Runtime is NOT actually atomic — cancel
        commits at tasks.py:444, then checklist writes commit
        separately (checklists.py:157,484). If add_item fails after
        cancel, the task is already cancelled with no rollback.
        Docstring rewritten to honestly expose the partial-failure
        risk (true transactional refactor is Phase B work).

        ОДИН вызов вместо двух (cancel_task + add_checklist_items) —
        короче для LLM. Используй когда:

        * Юзер просит «перенеси X из расписания в дела/чек-лист Y»
        * Понял что в прошлом turn'е ошибочно создал task вместо
          add_checklist_items, и хочешь перенести
        * Юзер уточнил «это не на конкретное время — переложи в дела»

        Что делает (последовательно, БЕЗ единой транзакции —
        each step commits independently):
        1. Cancel task (status='cancelled', commit). Linked reminder
           тоже отменяется.
        2. Resolve / create target checklist (commit).
        3. Add item с title задачи в target checklist с dedup
           защитой (commit). Если на этом шаге упало — task уже
           отменён, item не создан. Честно сообщи юзеру что перенос
           завершился частично.
        4. Если target checklist не найден — создаст его с title
           = list_id_or_title.

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
        # Codex Sub-A4 checklists R5 MINOR (HIGH-reasoning catch):
        # this lookup was outside try/except — if find_list_by_title
        # raised after task_service.cancel() committed, runtime
        # would bubble the exception as untyped error, hiding the
        # partial-failure semantic (task cancelled but list not
        # resolved). Now wraps to emit `error: list_resolve_failed`
        # which parser maps to typed MoveTaskPartialFailure.
        try:
            cl = checklist_service.find_list_by_title(
                tenant_id=tenant_id, user_id=user_id, needle=list_id_or_title,
            )
        except Exception:  # noqa: BLE001
            logger.exception("move_task_to_checklist: find_list_by_title failed")
            return "error: list_resolve_failed"
        if cl is None:
            try:
                cl = checklist_service.create_list(
                    tenant_id=tenant_id, user_id=user_id,
                    title=list_id_or_title,
                )
            except ValueError:
                # Codex Sub-A4 checklists R6 MINOR (HIGH catch):
                # ValueError after cancel committed (e.g.
                # `title required` for whitespace-only list_id_or_title)
                # used to return `error: {exc}` → untyped generic
                # error code at parse time. But task is ALREADY
                # cancelled here — this is a partial-failure scenario.
                # Remap to `list_resolve_failed` so parser routes to
                # typed MoveTaskPartialFailure.
                logger.exception("move_task_to_checklist: create_list ValueError")
                return "error: list_resolve_failed"
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
    def list_checklist_items(
        title_match: str, list_title_match: str | None = None,
    ) -> str:
        """List checklist items matching a title fragment, ACROSS lists.

        #143 Phase B: единообразно с задачами — читающий шаг возвращает
        ВСЕ подходящие пункты (фильтр-оставить-всех, НЕ top-1), дальше
        штатный ``.only`` выбирает ровно один (или честное уточнение
        из УЖЕ отфильтрованных кандидатов), а mark/delete мутируют по
        ``item_id``.

        Args:
            title_match: substring of the item title (case/ё-insensitive).
            list_title_match: optional — ограничить поиск списками, чьё
                название матчит этот фрагмент.

        Returns: multi-line listing or ``empty``. Per row:
        ``[<item_id>] <mark> <item_title> @ [<list_id>] <list_title>``.
        """
        if not user_id:
            return "error: no user_id context"
        lists = checklist_service.list_active(
            tenant_id=tenant_id, user_id=user_id,
        )
        if list_title_match:
            lists = [cl for cl in lists if _title_matches(list_title_match, cl.title)]
        lines: list[str] = []
        for cl in lists:
            for it in checklist_service.list_items(list_id=cl.id):
                if not _title_matches(title_match, it.title):
                    continue
                mark = {"pending": "☐", "done": "☑", "cancelled": "✗"}.get(
                    it.status, "?"
                )
                lines.append(
                    f"[{it.id}] {mark} {it.title} @ [{cl.id}] {cl.title}"
                )
        if not lines:
            return "empty"
        return "\n".join(lines)

    @_write_lc_tool
    def mark_checklist_item_done(item_id: str) -> str:
        """Mark one checklist item as done BY item_id.

        Use when user says «закройила лаванду», «купила сахар»,
        «сделал X». Сначала вызови list_checklist_items, чтобы получить
        item_id (``.only.item_id``), потом передай его сюда.

        Args:
            item_id: ``clitem_xxx`` id пункта (из list_checklist_items).

        Returns: ``ok:done:<item_id>:<title>`` or ``error:item_not_found``.
        """
        if not user_id:
            return "error: no user_id context"
        # АТОМАРНАЯ мутация: ownership (tenant+user) + активность списка +
        # mark в одном сервисном вызове (Codex review #143 B, MAJOR-1+2).
        # Раньше делали get_owned_item → mark_done двумя шагами: пункт мог
        # исчезнуть МЕЖДУ ними (гонка) → mark_done вернул None → отдавали
        # «error: internal» = «поломка»/неверный алертинг для класса
        # stale/not_found (критерий приёмки #8). Теперь None → мягкий
        # item_not_found. id не светим. Чужой/архивный/исчезнувший → None.
        done = checklist_service.mark_owned_item_done(
            item_id=(item_id or "").strip(), tenant_id=tenant_id, user_id=user_id,
        )
        if done is None:
            return "error: item_not_found"
        done_id, done_title = done
        return f"ok:done:{done_id}:{done_title}"

    @_write_lc_tool
    def delete_checklist_item(item_id: str) -> str:
        """Hard-delete one checklist item BY item_id (item is GONE).

        Use when user says «удали пункт X», «убери из списка Y»,
        «не то записала, удали» — pure correction, item disappears
        from all views. Differs from mark_checklist_item_done (status=
        done, ☑) and from archive_checklist (whole list out of view).

        Use this in particular when YOU (the AI) misheard / wrote a
        wrong item earlier in this conversation and the user asks
        to remove that wrong record — the previous bad item is gone,
        only the corrected one stays.

        Сначала вызови list_checklist_items, чтобы получить item_id
        (``.only.item_id``), потом передай его сюда.

        Args:
            item_id: ``clitem_xxx`` id пункта (из list_checklist_items).

        Returns: ``ok:deleted:<item_id>:<title>`` or ``error:item_not_found``.
        """
        if not user_id:
            return "error: no user_id context"
        # АТОМАРНОЕ удаление (см. mark_checklist_item_done): ownership +
        # активность + delete в одном вызове, без гонки check→delete.
        # None (чужой/архивный/исчезнувший) → мягкий item_not_found, НЕ
        # «error: internal» (Codex review #143 B, MAJOR-1+2).
        deleted = checklist_service.delete_owned_item(
            item_id=(item_id or "").strip(), tenant_id=tenant_id, user_id=user_id,
        )
        if deleted is None:
            return "error: item_not_found"
        item_id_resolved, item_title = deleted
        return f"ok:deleted:{item_id_resolved}:{item_title}"

    @_write_lc_tool
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
        update_reminder,
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
        # R-33 (2026-05-15, vex-assistant#42): task↔checklist link
        link_task_to_checklist,
        unlink_task,
        # Inline-кнопки (Часть 0 плана v2).
        reply_with_buttons,
        # Чек-листы — именованные списки дел с галочками (план 2026-04-25)
        create_checklist,
        add_checklist_items,
        list_checklists,
        show_checklist,
        # #143 Phase B: читающий шаг «по описанию» → .only → mark/delete по id
        list_checklist_items,
        mark_checklist_item_done,
        delete_checklist_item,
        archive_checklist,
        # 2026-04-28: атомарный перенос task → checklist
        move_task_to_checklist,
    ]
