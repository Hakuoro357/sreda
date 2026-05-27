"""ToolSpec instances for the REMINDERS family (Sub-A4 phase 2).

Each tool gets:
- ``input_model`` (pydantic, ``extra='forbid'``) — strict planner
  contract: exact keyword args the LLM may emit.
- ``output_model`` — discriminated union from ``housewife.py`` parser
  layer.
- ``trigger_examples`` — 3..10 Russian phrases for prompt few-shot.
- ``mutex_notes`` — disambiguation against close-sibling tools
  (schedule vs update vs cancel; tasks.attach_reminder for binding
  a NEW reminder to an existing task).
- ``family`` / ``effect`` / ``read_domains`` / ``write_domains`` —
  scheduler/router metadata.

Sources of truth (linked to keep schema↔runtime drift visible):

- Tool signatures: ``services/housewife_chat_tools.py:262`` (schedule),
  ``:432`` (list), ``:452`` (update), ``:531`` (cancel).
- Output shapes: ``services/tool_schemas/housewife.py``
  ``ScheduleReminderOutput`` / ``ListRemindersOutput`` /
  ``UpdateReminderOutput`` / ``CancelReminderOutput``.
- Family registration: ``tool_schemas/families.py:418-422`` —
  TOOL_FAMILY_MANIFEST already maps all 4 tools.

The python function implementations stay in
``housewife_chat_tools.py`` and produce ``str`` returns; the
``ToolSpec.output_model`` + matching parser in ``housewife.py``
typed-decode those strings for the executor.

Production policy validation happens via
``assert_production_registry_quality(REMINDERS_SPECS)`` — Sub-A4 CI
calls it to fail builds that ship incomplete reminders ToolSpecs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.common import (
    RecurrenceRule,
    ReminderId,
    ShortStr,
    TriggerIso,
)
from sreda.services.tool_schemas.housewife import (
    CancelReminderOutput,
    ListRemindersOutput,
    ScheduleReminderOutput,
    UpdateReminderOutput,
)


# ---------------------------------------------------------------------------
# Input models — strict planner contracts. Field names match
# ``housewife_chat_tools.py`` kwargs exactly.
# ---------------------------------------------------------------------------


class ScheduleReminderInput(BaseModel):
    """Schedule a new proactive reminder.

    Field caps (source: ``housewife_chat_tools.py:262``):
    - ``title``: non-blank short label ≤200 (matches the «под 200
      chars» docstring contract).
    - ``trigger_iso``: ``TriggerIso`` — ISO-8601 shape regex + ≤64
      chars. Runtime parses via ``datetime.fromisoformat`` and
      normalizes to UTC (offset-aware accepted; naive treated as
      UTC — see ``housewife_chat_tools.py:325-349``).
    - ``recurrence_rule``: optional ``RecurrenceRule`` — must start
      with ``FREQ=``, single-line, ≤512. Runtime hands to
      ``dateutil.rrule`` for full RFC-5545 validation.

    Codex Sub-A4 reminders R1 MAJOR #2 + #3: previously ``NonBlankStr``
    (unbounded, no shape constraint) — planner could emit huge or
    multiline values that surprised the runtime parser. Bounded
    aliases catch obvious mistakes at schema validation time.
    """

    model_config = ConfigDict(extra="forbid")
    title: ShortStr
    trigger_iso: TriggerIso
    recurrence_rule: RecurrenceRule | None = None


class ListRemindersInput(BaseModel):
    """No args — lists all pending reminders for the calling tenant."""
    model_config = ConfigDict(extra="forbid")


class UpdateReminderInput(BaseModel):
    """Update an existing reminder in place.

    ``required_any_non_null_args`` (via ToolSpec) enforces that at
    least one mutable field is provided non-null — same pattern as
    ``update_shopping_item`` (Codex Sub-A4 R4/R5/R6 MAJOR #1). The
    runtime (``housewife_chat_tools.py:494-528``) short-circuits on
    ``X is None`` for each mutable field, so a call with only
    ``reminder_id`` is a silent no-op.

    Codex Sub-A4 reminders R1 MAJOR #1: ``clear_recurrence`` was
    typed ``bool | None`` which let ``clear_recurrence=False`` slip
    past the no-op guard (False is non-null but runtime branches
    only on True). Tightened to ``Literal[True] | None = None`` —
    planner must either omit it or send True. Sending False is now
    a schema error.

    Codex Sub-A4 reminders R1 MAJOR #2 + #3: ``trigger_iso`` and
    ``recurrence_rule`` now use bounded shape-validated aliases
    (``TriggerIso`` / ``RecurrenceRule``) instead of the previous
    unbounded ``NonBlankStr``.
    """

    model_config = ConfigDict(extra="forbid")
    reminder_id: ReminderId
    title: ShortStr | None = None
    trigger_iso: TriggerIso | None = None
    recurrence_rule: RecurrenceRule | None = None
    clear_recurrence: Literal[True] | None = None


class CancelReminderInput(BaseModel):
    """Cancel a pending reminder by id."""

    model_config = ConfigDict(extra="forbid")
    reminder_id: ReminderId


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


SCHEDULE_REMINDER_SPEC = ToolSpec(
    name="schedule_reminder",
    description=(
        "Поставить напоминание на конкретное время в будущем. Используй "
        "когда юзер просит «напомни через N часов», «каждый вторник в "
        "16:00 пиши про X». ISO-8601 в UTC, относительные фразы резолви "
        "ДО вызова."
    ),
    family="reminders",
    effect="write",
    read_domains=[],
    write_domains=["reminders"],
    input_model=ScheduleReminderInput,
    output_model=ScheduleReminderOutput,
    trigger_examples=[
        "напомни через 2 часа купить хлеб",
        "напомни завтра в 9 утра позвонить врачу",
        "каждый вторник в 16:00 пиши про кружок",
        "поставь напоминание на пятницу 18:00",
        "напомни через полчаса проверить почту",
    ],
    mutex_notes=[
        # Codex Sub-A4 reminders R1 MAJOR #5: previously referenced
        # ``tasks.attach_reminder`` which is not yet in the typed
        # registry — planner would have been steered toward an
        # unavailable tool. Reword as intent-level guidance; restore
        # the cross-tool reference once tasks family migrates.
        "Используй для standalone-напоминаний (НЕ привязанных к существующей задаче). Если юзер просит «напомни про задачу T-X» — задача должна сама подцепить напоминание (legacy router пока).",
        "НЕ вызывай для уточнения существующего напоминания — это update_reminder, не schedule + cancel.",
        "Для отмены — cancel_reminder. Для проверки активных — list_reminders.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


LIST_REMINDERS_SPEC = ToolSpec(
    name="list_reminders",
    description=(
        "Показать все активные (pending) напоминания юзера со временем "
        "следующего срабатывания и recurrence. Используй когда юзер "
        "спрашивает «какие у меня напоминания?», «что я просил напомнить?»."
    ),
    family="reminders",
    effect="read",
    read_domains=["reminders"],
    write_domains=[],
    input_model=ListRemindersInput,
    output_model=ListRemindersOutput,
    trigger_examples=[
        "какие у меня напоминания",
        "что я просил напомнить",
        "покажи активные напоминания",
        "что мне напомнить сегодня",
    ],
    mutex_notes=[],
    timeout_seconds=5,
    side_effect_class="read_only",
)


UPDATE_REMINDER_SPEC = ToolSpec(
    name="update_reminder",
    description=(
        "Изменить существующее напоминание — текст, время или recurrence "
        "БЕЗ cancel+schedule цикла. Передавай ТОЛЬКО изменяющиеся поля; "
        "неуказанные остаются как есть."
    ),
    family="reminders",
    effect="write",
    read_domains=[],
    write_domains=["reminders"],
    input_model=UpdateReminderInput,
    output_model=UpdateReminderOutput,
    trigger_examples=[
        "увеличь напоминание до 20:00",
        "поменяй текст напоминания на «купить хлеб»",
        "напоминание не каждый час, а каждые 30 минут",
        "убери повтор у напоминания",
    ],
    mutex_notes=[
        "Используй для in-place правки. НЕ вызывай cancel + schedule — это две LLM-итерации и потеря recurrence-state.",
        "Сначала list_reminders чтобы получить reminder_id.",
        "Для отмены — cancel_reminder, не update с clear-всего.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
    # Codex Sub-A4 R4/R5/R6 MAJOR #1: runtime short-circuits on
    # ``X is None`` for every mutable field — so a call with only
    # ``reminder_id`` is a silent no-op. Phase 1.d in
    # runtime/planner/validator.py emits ``silent_noop_call`` when no
    # mutable field is provided non-null (refs and empty strings count
    # as non-null; explicit null and missing keys do not).
    #
    # ``clear_recurrence=False`` is also «no-op» semantically — the
    # runtime branch fires only on True. But it's a bool not a None,
    # so the rule «non-null» admits it; the planner is expected to
    # only emit ``clear_recurrence`` when it actually means True. If
    # this becomes a real failure mode in production we'll tighten
    # to «truthy» semantics; for MVP «non-null» is sufficient.
    required_any_non_null_args=[
        "title",
        "trigger_iso",
        "recurrence_rule",
        "clear_recurrence",
    ],
)


CANCEL_REMINDER_SPEC = ToolSpec(
    name="cancel_reminder",
    description=(
        "Отменить активное напоминание по id. Используй когда юзер "
        "говорит «отмени напоминание про X», «убери напоминание про Y», "
        "«не надо больше напоминать про Z»."
    ),
    family="reminders",
    effect="write",
    read_domains=[],
    write_domains=["reminders"],
    input_model=CancelReminderInput,
    output_model=CancelReminderOutput,
    trigger_examples=[
        "отмени напоминание про хлеб",
        "убери напоминание на завтра",
        "не надо больше напоминать про врача",
        "удали напоминание про кружок",
    ],
    mutex_notes=[
        "Используй для полной отмены. Для in-place правки — update_reminder, не cancel + schedule.",
        "Сначала list_reminders чтобы получить reminder_id.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


REMINDERS_SPECS: list[ToolSpec] = [
    SCHEDULE_REMINDER_SPEC,
    LIST_REMINDERS_SPEC,
    UPDATE_REMINDER_SPEC,
    CANCEL_REMINDER_SPEC,
]
"""All 4 reminders ToolSpec instances in stable order. Sub-A4 CI tests
construct this list and call ``assert_production_registry_quality``
on it to enforce the production-policy gate."""


__all__ = [
    "CANCEL_REMINDER_SPEC",
    "CancelReminderInput",
    "LIST_REMINDERS_SPEC",
    "ListRemindersInput",
    "REMINDERS_SPECS",
    "SCHEDULE_REMINDER_SPEC",
    "ScheduleReminderInput",
    "UPDATE_REMINDER_SPEC",
    "UpdateReminderInput",
]
