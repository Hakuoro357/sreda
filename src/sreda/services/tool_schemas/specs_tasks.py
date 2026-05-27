"""ToolSpec instances for the TASKS family (Sub-A4 phase 6).

11 tools migrated:
  - add_task / list_tasks / update_task
  - complete_task / uncomplete_task / cancel_task / delete_task
  - attach_reminder / detach_reminder
  - link_task_to_checklist / unlink_task

Sources of truth:
- Tool signatures: ``services/housewife_chat_tools.py:1790-2287``
- Output schemas: ``services/tool_schemas/housewife.py`` — 11 outputs,
  status tags align with runtime «ok:<verb>:...» tokens.
- ID factories: ``services/tasks.py:96,174`` (``task_<24 hex>``).
- task_id stable error pattern: housewife.py _STABLE_ERROR_PATTERNS
  already covers ``task_not_found`` from R1 shopping migration.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.common import (
    ChecklistId,
    RecurrenceRule,
    TaskId,
    validate_rrule_static,
)
from sreda.services.tool_schemas.housewife import (
    AddTaskOutput,
    AttachReminderOutput,
    CancelTaskOutput,
    CompleteTaskOutput,
    DeleteTaskOutput,
    DetachReminderOutput,
    LinkTaskToChecklistOutput,
    ListTasksOutput,
    UncompleteTaskOutput,
    UnlinkTaskOutput,
    UpdateTaskOutput,
)


# ---------------------------------------------------------------------------
# Tasks-specific aliases
# ---------------------------------------------------------------------------


TaskTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Task title — short imperative phrase («утренняя разминка», «встреча
с врачом»). 200 char cap matches the conservative upper limit on
short user phrases (longer goes into ``notes`` or ``details_items``)."""


TaskNotes = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
]
"""Free-form per-task note. Generous 2000-char cap because notes can
hold context like phone numbers, locations, prep instructions —
the planner shouldn't bounce off the limit on legitimate use."""


HHMM = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    ),
]
"""Local time HH:MM in user's timezone (housewife_chat_tools.py:1810).
Strict regex catches malformed «25:99» / «7:5» at planner time."""


def _validate_scheduled_date(value: str) -> str:
    """Accepts ``"today"`` / ``"tomorrow"`` / ``"inbox"`` / ISO date.
    ISO-shape gets semantic date check via ``date.fromisoformat``."""
    norm = value.strip().lower()
    if norm in {"today", "tomorrow", "inbox"}:
        return value.strip()
    # ISO date — let date.fromisoformat reject impossible like 2026-02-31
    _date.fromisoformat(value.strip())
    return value.strip()


ScheduledDate = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=32),
    AfterValidator(_validate_scheduled_date),
]
"""Task schedule slot: ``"today"`` / ``"tomorrow"`` / ``"inbox"`` /
ISO ``YYYY-MM-DD``. Mirrors ``_parse_task_date`` semantics at
housewife_chat_tools.py:1705-1722. Inbox means undated."""


ListTasksDateFilter = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=32),
    AfterValidator(_validate_scheduled_date),  # accepts the same set
]
"""list_tasks accepts the same date tokens AS WELL AS ``"all"`` —
covered below by a separate Literal-union for clarity."""


ListTasksDateArg = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=32),
]
"""list_tasks ``date`` field is union of schedule tokens + ``"all"``.
We validate via a model-level validator instead of fighting two
AfterValidator paths."""


def _validate_list_tasks_date(value: str) -> str:
    norm = value.strip().lower()
    if norm in {"today", "tomorrow", "inbox", "all"}:
        return value.strip()
    _date.fromisoformat(value.strip())
    return value.strip()


ListTasksDate = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=32),
    AfterValidator(_validate_list_tasks_date),
]


ListTasksStatus = Literal["pending", "completed", "all"]
"""Status filter for list_tasks (housewife_chat_tools.py:1973-1974)."""


DetailItem = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""One sub-item in the details_items composite path. Same cap as
title — these become checklist items («хлеб», «макароны»)."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class AddTaskInput(BaseModel):
    """Create a task. ``title`` mandatory; everything else optional.

    Codex Sub-A4 tasks R1 (preempting reviewer): cross-field rules
    mirror runtime guards at housewife_chat_tools.py:1884-1902:
    - ``reminder_offset_minutes`` requires both ``scheduled_date``
      (not ``"inbox"``) AND ``time_start``
    - ``reminder_offset_minutes`` and ``details_items`` are mutually
      exclusive (composite path doesn't call reminder attach)
    - ``recurrence_rule`` must be a valid RRULE (whitelisted FREQ
      + dateutil-parseable) via the shared reminder validator
    """

    model_config = ConfigDict(extra="forbid")
    title: TaskTitle
    scheduled_date: ScheduledDate | None = None
    time_start: HHMM | None = None
    time_end: HHMM | None = None
    recurrence_rule: RecurrenceRule | None = None
    notes: TaskNotes | None = None
    reminder_offset_minutes: int | None = Field(default=None, ge=1, le=10080)
    """Codex R1 MINOR #6: cap matches AttachReminderInput.offset_minutes
    (10080 min = 7 days). Reminding for something a week+ out is
    almost always a planner mistake."""
    details_items: list[DetailItem] | None = Field(default=None, min_length=1, max_length=50)
    """Codex R1 MAJOR #4: ``min_length=1`` rejects ``details_items=[]``.
    Empty list previously slipped through schema then was no-op'd by
    runtime ``if details_items:`` truthy check — planner couldn't tell
    «I asked for a checklist but it was dropped». ``None`` (default) is
    still the «no checklist» path; explicit empty list is malformed."""

    @model_validator(mode="after")
    def _validate_reminder_requires_schedule(self) -> "AddTaskInput":
        if self.reminder_offset_minutes is not None:
            if self.scheduled_date is None or self.scheduled_date.strip().lower() == "inbox":
                raise ValueError(
                    "reminder_offset_minutes requires scheduled_date "
                    "(not 'inbox') and time_start — runtime would reject."
                )
            if self.time_start is None:
                raise ValueError(
                    "reminder_offset_minutes requires time_start — "
                    "no point reminding without a fire time."
                )
        return self

    @model_validator(mode="after")
    def _validate_reminder_xor_details(self) -> "AddTaskInput":
        if self.reminder_offset_minutes is not None and self.details_items:
            raise ValueError(
                "reminder_offset_minutes is incompatible with details_items "
                "— the composite create path doesn't attach reminders. "
                "Create the task with details_items first, then "
                "attach_reminder(task_id, offset_minutes) separately."
            )
        return self

    @model_validator(mode="after")
    def _validate_time_end_after_start(self) -> "AddTaskInput":
        if self.time_start and self.time_end and self.time_end <= self.time_start:
            raise ValueError(
                f"time_end={self.time_end!r} must be after "
                f"time_start={self.time_start!r}. Use 24h HH:MM."
            )
        return self


class ListTasksInput(BaseModel):
    """Filter tasks by date and status. Defaults match runtime
    («today» / «pending»)."""

    model_config = ConfigDict(extra="forbid")
    date: ListTasksDate = "today"
    status: ListTasksStatus = "pending"


class UpdateTaskInput(BaseModel):
    """Patch one task. ``task_id`` required; at least one other
    field non-None (planner mistake otherwise — runtime would
    return ``ok:updated`` with zero changes)."""

    model_config = ConfigDict(extra="forbid")
    task_id: TaskId
    title: TaskTitle | None = None
    scheduled_date: ScheduledDate | None = None
    time_start: HHMM | None = None
    time_end: HHMM | None = None
    recurrence_rule: RecurrenceRule | None = None
    notes: TaskNotes | None = None

    @model_validator(mode="after")
    def _validate_at_least_one_field(self) -> "UpdateTaskInput":
        if all(
            getattr(self, f) is None
            for f in (
                "title",
                "scheduled_date",
                "time_start",
                "time_end",
                "recurrence_rule",
                "notes",
            )
        ):
            raise ValueError(
                "UpdateTaskInput requires at least one updatable field "
                "(title/scheduled_date/time_start/time_end/recurrence_rule/"
                "notes). Empty payload would be a no-op."
            )
        return self

    @model_validator(mode="after")
    def _validate_time_end_after_start(self) -> "UpdateTaskInput":
        if self.time_start and self.time_end and self.time_end <= self.time_start:
            raise ValueError(
                f"time_end={self.time_end!r} must be after "
                f"time_start={self.time_start!r}."
            )
        return self

    @model_validator(mode="after")
    def _reject_inbox_in_update(self) -> "UpdateTaskInput":
        """Codex Sub-A4 tasks R1 MAJOR #2: runtime ``TaskService.update``
        treats ``scheduled_date=None`` as «leave as-is», not «clear
        date to inbox». Passing ``scheduled_date="inbox"`` here would
        become a successful no-op — planner thinks it moved the task
        to inbox, runtime did nothing. Reject explicitly until the
        runtime gains an explicit clear-date sentinel (analogous to
        the household ``clear_birth_year`` flag from R3)."""
        if (
            self.scheduled_date is not None
            and self.scheduled_date.strip().lower() == "inbox"
        ):
            raise ValueError(
                "scheduled_date='inbox' is not supported by update_task — "
                "runtime cannot clear a task's schedule via this path. "
                "Either keep the existing date, or delete_task + add_task "
                "(inbox) to move the task to inbox."
            )
        return self


class CompleteTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: TaskId


class UncompleteTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: TaskId


class CancelTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: TaskId


class DeleteTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: TaskId


class AttachReminderInput(BaseModel):
    """Add a reminder to an existing task — N minutes before task's
    ``time_start``. Task must already have schedule_date + time_start."""

    model_config = ConfigDict(extra="forbid")
    task_id: TaskId
    offset_minutes: int = Field(ge=1, le=10080)
    """1 minute lower bound (runtime guard); 7 days upper (10080 mins)
    — reminding for something a week+ out is almost always a planner
    mistake; let it reject upstream so it's caught."""


class DetachReminderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: TaskId


class LinkTaskToChecklistInput(BaseModel):
    """Explicit 1-to-1 attach. For new task + details in one shot use
    add_task(details_items=...) instead — composite path is atomic."""

    model_config = ConfigDict(extra="forbid")
    task_id: TaskId
    checklist_id: ChecklistId


class UnlinkTaskInput(BaseModel):
    """Idempotent — runtime emits ok:not_linked when there's nothing
    to detach."""

    model_config = ConfigDict(extra="forbid")
    task_id: TaskId


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


ADD_TASK_SPEC = ToolSpec(
    name="add_task",
    description=(
        "Создать задачу в Расписании юзера: «поставь задачу X», "
        "«добавь Y в расписание», «запиши на завтра Z». Поля: title "
        "(краткое имя), scheduled_date (today/tomorrow/ISO/inbox), "
        "time_start/time_end (HH:MM в зоне юзера), recurrence_rule "
        "(RRULE с UTC BYHOUR), notes, reminder_offset_minutes "
        "(минуты ДО time_start), details_items (под-чеклист). ВАЖНО: "
        "reminder требует scheduled_date+time_start; reminder + "
        "details_items НЕ совместимы (сначала add_task с details, "
        "потом attach_reminder отдельным вызовом). details_items "
        "только если один verb + список объектов: «купи хлеб, "
        "макароны, яйца» → title=«Купить», details=[...]. Разные "
        "verbs («купи X, помой Y») → ОТДЕЛЬНЫЕ add_task calls."
    ),
    family="tasks",
    effect="write",
    read_domains=[],
    write_domains=["tasks", "checklists", "reminders"],
    input_model=AddTaskInput,
    output_model=AddTaskOutput,
    trigger_examples=[
        "поставь задачу позвонить врачу завтра в 14",
        "добавь в расписание тренировку понедельник 7 утра",
        "запиши: купить хлеб, макароны, яйца на завтра",
        "запиши встречу с врачом на пятницу 15:00 с напоминанием за 30 минут",
    ],
    mutex_notes=[
        "Создание. Для правки → update_task. Для отметки выполненной → complete_task. Для отмены → cancel_task. Для удаления → delete_task.",
        "details_items только для same-verb + список объектов. Разные действия → несколько add_task calls.",
        "reminder + details_items НЕ совместимы — сначала add_task с details, потом attach_reminder отдельно.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


LIST_TASKS_SPEC = ToolSpec(
    name="list_tasks",
    description=(
        "Показать задачи юзера. Фильтры: date (today/tomorrow/ISO/"
        "inbox/all) — default «today»; status (pending/completed/all) "
        "— default «pending». Возвращает многострочный текстовый "
        "dump с task_id для последующих update/complete/cancel. "
        "Пусто → «no tasks» (статус=empty). Используй ПЕРЕД "
        "update_task/complete_task/cancel_task/delete_task если "
        "юзер называет задачу по имени, а не по id."
    ),
    family="tasks",
    effect="read",
    read_domains=["tasks"],
    write_domains=[],
    input_model=ListTasksInput,
    output_model=ListTasksOutput,
    trigger_examples=[
        "что у меня на сегодня",
        "покажи задачи на завтра",
        "что в инбоксе",
        "какие задачи выполнены",
    ],
    mutex_notes=[
        "Возвращает только Расписание — не покупки, не меню, не семью. Для покупок → list_shopping, для меню → list_menu.",
        "Используй чтобы получить task_id перед update/complete/cancel/delete когда юзер назвал задачу по имени.",
    ],
    timeout_seconds=5,
    side_effect_class="read_only",
)


UPDATE_TASK_SPEC = ToolSpec(
    name="update_task",
    description=(
        "Изменить поля существующей задачи: «перенеси на завтра», "
        "«сделай ежедневной», «поменяй время на 8:00». Передавай "
        "ТОЛЬКО те поля что меняются. Минимум одно поле должно быть "
        "non-None (пустой апдейт = no-op runtime). Если меняешь "
        "scheduled_date/time_start — линкованный reminder "
        "автоматически перепланируется. Чтобы добавить/убрать "
        "сам reminder — attach_reminder/detach_reminder. Если "
        "task_id ещё нет — сначала list_tasks. ВНИМАНИЕ: "
        "scheduled_date='inbox' через update НЕ поддерживается "
        "(runtime трактует как «оставить как было»). Для переноса "
        "в inbox — delete_task + add_task без scheduled_date."
    ),
    family="tasks",
    effect="write",
    read_domains=[],
    write_domains=["tasks", "reminders"],
    input_model=UpdateTaskInput,
    output_model=UpdateTaskOutput,
    trigger_examples=[
        "перенеси утреннюю разминку на 8 часов",
        "поменяй задачу X на завтра",
        "сделай ежедневной",
        "добавь заметку к задаче — взять документы",
    ],
    mutex_notes=[
        "Используй для ИЗМЕНЕНИЯ существующей задачи. Создание → add_task. Отметка готовой → complete_task.",
        "task_id берётся из list_tasks. attach_reminder/detach_reminder для самого факта напоминания (а не его времени).",
    ],
    # Codex Sub-A4 tasks R1 MAJOR #1: model_validator
    # `_validate_at_least_one_field` in UpdateTaskInput catches direct
    # planner construction, but the plan validator skips Pydantic
    # model_validators when args contain refs
    # (${list_tasks.tasks[0].task_id} etc). required_any_non_null_args
    # runs at the validator-driven Phase 1.d layer BEFORE Pydantic
    # construction, so refs-bearing no-op updates are caught. Same
    # pattern as reminders R3 (update_reminder).
    required_any_non_null_args=[
        "title",
        "scheduled_date",
        "time_start",
        "time_end",
        "recurrence_rule",
        "notes",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


COMPLETE_TASK_SPEC = ToolSpec(
    name="complete_task",
    description=(
        "Отметить задачу как выполненную: «сделал X», «закончил Y», "
        "«готово». Для one-shot задач с reminder — reminder "
        "автоматически отменяется. Для recurring — reminder остаётся "
        "(завтрашнее повторение всё равно сработает). Если task_id "
        "ещё нет — list_tasks сначала."
    ),
    family="tasks",
    effect="write",
    read_domains=[],
    write_domains=["tasks", "reminders"],
    input_model=CompleteTaskInput,
    output_model=CompleteTaskOutput,
    trigger_examples=[
        "сделал утреннюю разминку",
        "закончил с задачей покупки",
        "готово — звонил врачу",
        "выполнил всё что планировал",
    ],
    mutex_notes=[
        "Используй ТОЛЬКО для «выполнил». Для «передумал делать» — cancel_task. Для «совсем убери» — delete_task. Для отката completion — uncomplete_task.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


UNCOMPLETE_TASK_SPEC = ToolSpec(
    name="uncomplete_task",
    description=(
        "Откатить отметку «выполнено» — задача снова pending. Если "
        "при completion был cancelled reminder — он НЕ восстанавливается "
        "(юзер может attach_reminder заново). Используй когда юзер "
        "говорит «не, ещё не сделал», «вернул в дела»."
    ),
    family="tasks",
    effect="write",
    read_domains=[],
    write_domains=["tasks"],
    input_model=UncompleteTaskInput,
    output_model=UncompleteTaskOutput,
    trigger_examples=[
        "не, разминку ещё не сделал, верни в pending",
        "ошибся, не выполнил эту задачу",
        "верни в дела",
        "отметь обратно как невыполненную",
    ],
    mutex_notes=[
        "Откат именно от completion. Для отмены задачи целиком → cancel_task. Для удаления → delete_task.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


CANCEL_TASK_SPEC = ToolSpec(
    name="cancel_task",
    description=(
        "Отменить задачу (soft) — строка остаётся в БД со "
        "status=cancelled, исчезает из pending-списков. Linked "
        "reminder отменяется. Используй когда юзер «отмени эту "
        "задачу», «передумал делать», но НЕ просит совсем удалить. "
        "Для совсем удалить → delete_task."
    ),
    family="tasks",
    effect="write",
    read_domains=[],
    write_domains=["tasks", "reminders"],
    input_model=CancelTaskInput,
    output_model=CancelTaskOutput,
    trigger_examples=[
        "отмени задачу про доктора",
        "передумал, убери эту встречу",
        "отмени тренировку завтра",
        "сними со счёта эту задачу",
    ],
    mutex_notes=[
        "Soft-отмена. Hard-удаление → delete_task. Отметка выполненной → complete_task.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


DELETE_TASK_SPEC = ToolSpec(
    name="delete_task",
    description=(
        "Удалить задачу полностью (hard) — строка пропадает из БД, "
        "восстановлению не подлежит. Linked reminder отменяется. "
        "Используй ТОЛЬКО когда юзер явно «убери совсем», «удали», "
        "«вычеркни»."
    ),
    family="tasks",
    effect="write",
    read_domains=[],
    write_domains=["tasks", "reminders"],
    input_model=DeleteTaskInput,
    output_model=DeleteTaskOutput,
    trigger_examples=[
        "удали задачу про разминку совсем",
        "убери эту задачу из истории",
        "вычеркни задачу",
        "удали — больше не нужна",
    ],
    mutex_notes=[
        "ТОЛЬКО когда юзер просит «удали совсем». Для отмены без удаления → cancel_task. Для отметки выполненной → complete_task.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


ATTACH_REMINDER_SPEC = ToolSpec(
    name="attach_reminder",
    description=(
        "Привязать reminder к существующей задаче (за N минут до "
        "time_start). Используй когда юзер ответил «да, за N минут» "
        "на пост-creation вопрос «нужно ли напоминание». Требует "
        "чтобы задача имела scheduled_date + time_start (inbox-задачи "
        "не годятся). offset_minutes от 1 до 10080 (неделя)."
    ),
    family="tasks",
    effect="write",
    read_domains=["tasks"],
    write_domains=["reminders", "tasks"],
    input_model=AttachReminderInput,
    output_model=AttachReminderOutput,
    trigger_examples=[
        "да, поставь напоминание за 15 минут",
        "напомни за полчаса до этой задачи",
        "добавь reminder за 5 минут",
        "разбуди меня за час до встречи",
    ],
    mutex_notes=[
        "Привязка reminder к задаче. Снятие → detach_reminder. Создание задачи С reminder сразу → add_task с reminder_offset_minutes (если без details_items).",
        "Для standalone reminder без задачи → schedule_reminder из группы НАПОМИНАНИЯ.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


DETACH_REMINDER_SPEC = ToolSpec(
    name="detach_reminder",
    description=(
        "Убрать reminder с задачи (linked FamilyReminder отменяется). "
        "Сама задача остаётся. Используй когда юзер «убери напоминание "
        "с этой задачи», «не нужен reminder больше»."
    ),
    family="tasks",
    effect="write",
    read_domains=["tasks"],
    write_domains=["reminders", "tasks"],
    input_model=DetachReminderInput,
    output_model=DetachReminderOutput,
    trigger_examples=[
        "убери напоминание с этой задачи",
        "не нужен reminder больше",
        "сними напоминание у разминки",
        "детач reminder",
    ],
    mutex_notes=[
        "Идемпотентно — runtime просто отменит reminder если он был. Задача остаётся.",
        "Для удаления задачи целиком → delete_task. Для отмены задачи → cancel_task.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


LINK_TASK_TO_CHECKLIST_SPEC = ToolSpec(
    name="link_task_to_checklist",
    description=(
        "Связать существующую задачу с существующим чек-листом "
        "(R-33: явная 1-to-1 связь). Используй когда оба уже "
        "созданы отдельно и юзер просит связать («прикрепи список Y "
        "к задаче X»). Для создания НОВОЙ task с деталями в одной "
        "транзакции → add_task(details_items=...) вместо этого tool. "
        "Возможные статусы: linked (новая связь), already_linked (та "
        "же пара уже была — идемпотентно), error: "
        "task_already_linked (задача связана с другим checklist'ом, "
        "unlink сначала), error: checklist_already_linked (checklist "
        "на другой задаче)."
    ),
    family="tasks",
    effect="write",
    read_domains=["tasks", "checklists"],
    write_domains=["tasks", "checklists"],
    input_model=LinkTaskToChecklistInput,
    output_model=LinkTaskToChecklistOutput,
    trigger_examples=[
        "прикрепи список покупок к задаче поход в магазин",
        "свяжи checklist уборка с задачей субботняя уборка",
        "линкуй этот список к этой задаче",
        "привяжи задачу к чеклисту с шагами",
    ],
    mutex_notes=[
        "Используй для СВЯЗКИ существующих task+checklist. Для создания task с deталями сразу → add_task(details_items=...). Для разрыва связи → unlink_task.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


UNLINK_TASK_SPEC = ToolSpec(
    name="unlink_task",
    description=(
        "Отвязать задачу от чеклиста (R-33: разорвать связь). Task "
        "и checklist оба остаются как отдельные сущности, только "
        "break link. Идемпотентно: если task не была linked — "
        "возвращается ok:not_linked (не ошибка). Используй когда "
        "юзер «отвяжи список от задачи», «убери связь»."
    ),
    family="tasks",
    effect="write",
    read_domains=["tasks"],
    write_domains=["tasks", "checklists"],
    input_model=UnlinkTaskInput,
    output_model=UnlinkTaskOutput,
    trigger_examples=[
        "отвяжи список от задачи",
        "убери связь между задачей и checklist",
        "разорви линк",
        "не нужна связь больше",
    ],
    mutex_notes=[
        "Идемпотентный unlink. Восстановить связь → link_task_to_checklist. Удаление самого чеклиста — отдельный инструмент из группы ЧЕК-ЛИСТЫ.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


TASKS_SPECS: list[ToolSpec] = [
    ADD_TASK_SPEC,
    LIST_TASKS_SPEC,
    UPDATE_TASK_SPEC,
    COMPLETE_TASK_SPEC,
    UNCOMPLETE_TASK_SPEC,
    CANCEL_TASK_SPEC,
    DELETE_TASK_SPEC,
    ATTACH_REMINDER_SPEC,
    DETACH_REMINDER_SPEC,
    LINK_TASK_TO_CHECKLIST_SPEC,
    UNLINK_TASK_SPEC,
]


__all__ = [
    "ADD_TASK_SPEC",
    "ATTACH_REMINDER_SPEC",
    "AddTaskInput",
    "AttachReminderInput",
    "CANCEL_TASK_SPEC",
    "COMPLETE_TASK_SPEC",
    "CancelTaskInput",
    "CompleteTaskInput",
    "DELETE_TASK_SPEC",
    "DETACH_REMINDER_SPEC",
    "DeleteTaskInput",
    "DetachReminderInput",
    "DetailItem",
    "HHMM",
    "LINK_TASK_TO_CHECKLIST_SPEC",
    "LIST_TASKS_SPEC",
    "LinkTaskToChecklistInput",
    "ListTasksDate",
    "ListTasksInput",
    "ListTasksStatus",
    "ScheduledDate",
    "TASKS_SPECS",
    "TaskNotes",
    "TaskTitle",
    "UNCOMPLETE_TASK_SPEC",
    "UNLINK_TASK_SPEC",
    "UPDATE_TASK_SPEC",
    "UncompleteTaskInput",
    "UnlinkTaskInput",
    "UpdateTaskInput",
]
