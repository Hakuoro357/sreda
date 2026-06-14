"""ToolSpec instances for the CHECKLISTS family (Sub-A4 phase 7).

8 tools migrated:
  - create_checklist
  - add_checklist_items
  - move_task_to_checklist (cross-family: tasks → checklists)
  - list_checklists
  - show_checklist
  - mark_checklist_item_done
  - delete_checklist_item
  - archive_checklist

Sources of truth:
- Tool signatures: ``services/housewife_chat_tools.py:2368-2698``
- Output schemas: ``services/tool_schemas/housewife.py`` — 8 outputs
- ID factories: ``services/checklists.py:148,241`` (``checklist_<24 hex>``),
  ``:301,471`` (``clitem_<24 hex>``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.common import ChecklistItemId, ChecklistId, TaskId
from sreda.services.tool_schemas.housewife import (
    AddChecklistItemsOutput,
    ArchiveChecklistOutput,
    CreateChecklistOutput,
    DeleteChecklistItemOutput,
    ListChecklistItemsOutput,
    ListChecklistsOutput,
    MarkChecklistItemDoneOutput,
    MoveTaskToChecklistOutput,
    ShowChecklistOutput,
)


# ---------------------------------------------------------------------------
# Checklists-specific aliases
# ---------------------------------------------------------------------------


ChecklistTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Checklist name — short phrase («План кроя на эту неделю», «Дела
по машине»). 200 char cap matches typical user dictation length."""


def _coerce_title_object(v: object) -> object:
    """#124 ход 1 (прод-сессия 2026-06-10): планировщик кладёт пункты
    чек-листа объектами ``{"title": "Обувь"}`` вместо строк → нарушения
    валидатора → invalid_plan_after_retry → семья без чек-листа.
    Коэрцируем ТОЛЬКО чистую форму ``{"title": str}`` (единственный
    ключ): объект с дополнительными ключами (``{"title": …, "count": 5}``)
    оставляем как есть — молчаливый дроп лишних полей был бы потерей
    данных, пусть ретрай-фидбек учит модель строкам.

    Живёт В АННОТАЦИИ (BeforeValidator), а не ``@field_validator`` модели:
    плановый валидатор гоняет поля через ``TypeAdapter(annotation)`` —
    модельные валидаторы туда не попадают (страж в ToolSpec это
    запрещает), а метаданные Annotated проходят оба пути."""
    if isinstance(v, dict) and set(v.keys()) == {"title"} and isinstance(
        v["title"], str
    ):
        return v["title"]
    return v


ChecklistItemTitle = Annotated[
    str,
    BeforeValidator(_coerce_title_object),
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]
"""One item inside a checklist. Wider than title cap because items
can carry more detail («Лаванда 298 ТС, простыня 141×200×19»).
Принимает и ``{"title": str}`` (коэрция, см. ``_coerce_title_object``)."""


ListIdOrTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Polymorphic input: either a ``checklist_<24 hex>`` id OR a fuzzy
title fragment for runtime ``find_list_by_title`` lookup
(housewife_chat_tools.py:2433,2519,2586,2619,2658,2688). Runtime
disambiguates by trying ``startswith('checklist_')`` first."""


ItemTitleMatch = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Substring used by list_checklist_items to fuzzy-locate items by title
ACROSS active lists (#143 Phase B читающий шаг → .only → mark/delete по id)."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class CreateChecklistInput(BaseModel):
    """Create a NAMED empty checklist. Use add_checklist_items for
    populating + auto-create combined."""

    model_config = ConfigDict(extra="forbid")
    title: ChecklistTitle


class AddChecklistItemsInput(BaseModel):
    """Add items to a checklist (creates it if missing).

    Runtime guarantees: empty items list rejected as
    ``error: empty items``; whitespace-only entries filtered.
    Schema enforces non-empty list + per-item content."""

    model_config = ConfigDict(extra="forbid")
    list_id_or_title: ListIdOrTitle
    items: list[ChecklistItemTitle] = Field(min_length=1, max_length=100)


class MoveTaskToChecklistInput(BaseModel):
    """Task → checklist move (best-effort, not atomic — see
    MOVE_TASK_TO_CHECKLIST_SPEC.description). Cancels source task,
    dedup-adds to target. Cross-family tool (tasks domain →
    checklists domain). Failure between cancel and add returns
    typed ``MoveTaskPartialFailure``."""

    model_config = ConfigDict(extra="forbid")
    task_id: TaskId
    list_id_or_title: ListIdOrTitle


class ListChecklistsInput(BaseModel):
    """No arguments. Runtime returns all active checklists with
    item counts."""

    model_config = ConfigDict(extra="forbid")


class ShowChecklistInput(BaseModel):
    """Display all items in one checklist with their statuses."""

    model_config = ConfigDict(extra="forbid")
    list_id_or_title: ListIdOrTitle


class ListChecklistItemsInput(BaseModel):
    """#143 Phase B: найти пункты по фрагменту названия СРАЗУ ВО ВСЕХ
    активных списках (или в списках, чьё название матчит
    ``list_title_match``). Читающий шаг под id-путь mark/delete: выдача —
    ВСЕ совпадения, дальше штатный ``.only`` выбирает ровно один."""

    model_config = ConfigDict(extra="forbid")
    title_match: ItemTitleMatch
    list_title_match: ListIdOrTitle | None = None


class MarkChecklistItemDoneInput(BaseModel):
    """#143 Phase B: отметить пункт «сделано» строго по ``item_id``.
    Планировщик получает id из list_checklist_items (``.only.item_id``) —
    title-пути у планировщика больше НЕТ (отрицательная гарантия)."""

    model_config = ConfigDict(extra="forbid")
    item_id: ChecklistItemId


class DeleteChecklistItemInput(BaseModel):
    """#143 Phase B: жёстко удалить пункт строго по ``item_id``.
    Планировщик получает id из list_checklist_items (``.only.item_id``) —
    title-пути у планировщика больше НЕТ (отрицательная гарантия)."""

    model_config = ConfigDict(extra="forbid")
    item_id: ChecklistItemId


class ArchiveChecklistInput(BaseModel):
    """Archive the whole checklist — disappears from list_checklists
    but rows stay in DB for recall."""

    model_config = ConfigDict(extra="forbid")
    list_id_or_title: ListIdOrTitle


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


CREATE_CHECKLIST_SPEC = ToolSpec(
    name="create_checklist",
    description=(
        "Создать ИМЕНОВАННЫЙ чек-лист (todo с галочками) БЕЗ пунктов. "
        "ТОЛЬКО когда юзер явно «заведи пустой список Y». Обычно юзер хочет "
        "сразу С ПУНКТАМИ — тогда add_checklist_items (сам создаст список). "
        "Возвращает ok:created:checklist_X:title."
    ),
    family="checklists",
    effect="write",
    # Codex Sub-A4 checklists R2 MINOR (new): runtime
    # ChecklistService.create_list reads active checklists to dedup
    # by title before writing (checklists.py:133). read_domains
    # declares «every mutable data store touched» — checklists is
    # read for dedup even though the read isn't a separate tool call.
    read_domains=["checklists"],
    write_domains=["checklists"],
    input_model=CreateChecklistInput,
    output_model=CreateChecklistOutput,
    trigger_examples=[
        "заведи пустой список «Дача»",
        "создай чек-лист «План кроя» без пунктов",
        "новый пустой список — поездка",
        "сделай пустой checklist для ремонта",
    ],
    mutex_notes=[
        "ТОЛЬКО пустой список. С пунктами — add_checklist_items. Покупки — add_shopping_items. Задачи с временем — add_task.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


ADD_CHECKLIST_ITEMS_SPEC = ToolSpec(
    name="add_checklist_items",
    description=(
        # P1 2026-06-11 prompt_budget_exceeded (прод -8 симв.): описание
        # ужато; смысловые потери — только дубль mutex-note про
        # create_checklist. Привязка эха ТОЛЬКО для status=added: при
        # added_with_dups created может быть пуст (всё дубли) —
        # checklist_show спрятал бы «уже было» (Codex R2 MAJOR).
        "Добавить пункты в чек-лист (сам создаст список, если такого "
        "нет). Триггеры: «запиши в дела по машине: …», «план кроя: …». "
        "Дубли не ошибка — идут в dups count. Возвращает "
        "ok:added:N:list=<id> или ok:added:N:dups:M:list=<id>. "
        "items: строки. status=added → checklist_show: "
        "{\"title\": <имя>, \"items\": \"${sN.created}\"}."
    ),
    family="checklists",
    effect="write",
    read_domains=["checklists"],
    write_domains=["checklists"],
    input_model=AddChecklistItemsInput,
    output_model=AddChecklistItemsOutput,
    trigger_examples=[
        "запиши в дела по машине: колодки, стекло, масло",
        "добавь в дела на дачу: привезти лопату, купить рассаду",
        "план кроя на эту неделю: лаванда 298, шампань 202, жемчуг 156",
        "запиши в материалы для ремонта: краска, валик, скотч",
    ],
    mutex_notes=[
        "Auto-create + populate. Пустой список → create_checklist. Покупки → add_shopping_items. Задачи с датой → add_task. Дубли НЕ ошибка (dups count), не ретраить.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


MOVE_TASK_TO_CHECKLIST_SPEC = ToolSpec(
    name="move_task_to_checklist",
    # #128: ужато; граница с link_task_to_checklist — в mutex_notes
    description=(
        "Перенести задачу из Расписания в чек-лист как ПУНКТ — один "
        "вызов вместо cancel_task + add_checklist_items: «перенеси X в "
        "дела Y», «это не на время — переложи в дела». Best-effort (НЕ "
        "одна транзакция): cancel task → add item (target создаётся, "
        "если нет). Если add упал — task уже отменён: скажи о частичном "
        "переносе. Идемпотентно (повтор → moved_dup)."
    ),
    family="checklists",
    effect="write",
    read_domains=["tasks", "checklists"],
    write_domains=["tasks", "checklists", "reminders"],
    input_model=MoveTaskToChecklistInput,
    output_model=MoveTaskToChecklistOutput,
    trigger_examples=[
        "перенеси задачу X из расписания в чек-лист дача",
        "это не на время — переложи в дела на даче",
        "сверни задачу про лекарства в пункт checklist",
        "сделай эту задачу пунктом в плане кроя",
    ],
    mutex_notes=[
        "task → пункт чек-листа (task cancelled). Логическая СВЯЗЬ (оба остаются) — link_task_to_checklist. Идемпотентно: дубль не создаётся (moved_dup).",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


LIST_CHECKLISTS_SPEC = ToolSpec(
    name="list_checklists",
    description=(
        "Показать все активные чек-листы юзера со счётчиками pending/done/total. "
        "Триггеры: «какие у меня списки», «покажи все мои планы», «покажи дела». "
        "checklist_id годен для show_checklist / archive_checklist. Пусто → "
        "empty. Показ — checklists_list_show: {\"items\": "
        "\"${sN.checklists}\"}; пусто → checklists_list_empty."
    ),
    family="checklists",
    effect="read",
    read_domains=["checklists"],
    write_domains=[],
    input_model=ListChecklistsInput,
    output_model=ListChecklistsOutput,
    trigger_examples=[
        "какие у меня списки",
        "покажи все мои чек-листы",
        "что у меня в планах",
        "сколько списков я завёл",
    ],
    mutex_notes=[
        "Возвращает СПИСКИ юзера, не их содержимое. Для пунктов конкретного списка — show_checklist.",
    ],
    timeout_seconds=5,
    side_effect_class="read_only",
)


SHOW_CHECKLIST_SPEC = ToolSpec(
    name="show_checklist",
    description=(
        "Показать ПУНКТЫ одного чек-листа со статусами (pending/done/cancelled). "
        "Триггеры: «покажи план кроя», «что осталось в списке X», «что я ещё "
        "не сделал». Принимает checklist_<id> или нечёткий title. Пунктов нет "
        "→ empty (список есть — это НЕ not_found). "
        # P1 2026-06-11: items чек-листа собрали reminders_list_show →
        # crash рендера (нет display_line). Показ — ТОЛЬКО checklist_show.
        "Показ — checklist_show: {\"title\": \"${sN.title}\", \"items\": "
        "\"${sN.items}\"} (НЕ reminders_list_show/shopping_list_show)."
    ),
    family="checklists",
    effect="read",
    read_domains=["checklists"],
    write_domains=[],
    input_model=ShowChecklistInput,
    output_model=ShowChecklistOutput,
    trigger_examples=[
        "покажи план кроя",
        "что осталось в списке дача",
        "что я ещё не сделал из плана",
        "открой checklist по машине",
    ],
    mutex_notes=[
        "Пункты ОДНОГО названного списка. Все списки — list_checklists. Пункт по описанию для mark/delete — list_checklist_items.",
    ],
    timeout_seconds=5,
    side_effect_class="read_only",
)


LIST_CHECKLIST_ITEMS_SPEC = ToolSpec(
    name="list_checklist_items",
    description=(
        "Найти пункты по фрагменту названия во ВСЕХ активных списках "
        "(сузь list_title_match). Читающий шаг под mark/delete ПО ИМЕНИ "
        "(«отметь лопату», «удали стекло»): передай ${sN.items.only.item_id} "
        "в mark_checklist_item_done / delete_checklist_item. Отдаёт ВСЕ "
        "совпадения (не топ-1), .only выберет один. Пусто → empty. "
        "Показ — checklist_items_show / _empty."
    ),
    family="checklists",
    effect="read",
    read_domains=["checklists"],
    write_domains=[],
    input_model=ListChecklistItemsInput,
    output_model=ListChecklistItemsOutput,
    trigger_examples=[
        "найди пункт лопата в моих списках",
        "в каком списке у меня лаванда",
        "найди пункт стекло чтобы удалить",
        "покажи где записана колодка",
    ],
    mutex_notes=[
        "Пункты по имени из РАЗНЫХ списков (.only → mark/delete по item_id). Пункты ОДНОГО названного списка — show_checklist; список списков — list_checklists.",
    ],
    timeout_seconds=5,
    side_effect_class="read_only",
)


MARK_CHECKLIST_ITEM_DONE_SPEC = ToolSpec(
    name="mark_checklist_item_done",
    description=(
        "Отметить пункт чек-листа сделанным по item_id (id из "
        "list_checklist_items → ${sN.items.only.item_id}). Триггеры: "
        "«сделал X», «купила Y», «закройла лаванду». Возвращает "
        "ok:done:<item>:title или error:checklist_item_not_found."
    ),
    family="checklists",
    effect="write",
    read_domains=["checklists"],
    write_domains=["checklists"],
    input_model=MarkChecklistItemDoneInput,
    output_model=MarkChecklistItemDoneOutput,
    trigger_examples=[
        "сделал лаванду в плане кроя",
        "закройла шампань",
        "купила колодки",
        "выполнила пункт про лопату",
    ],
    mutex_notes=[
        "«Сделано» по item_id (из list_checklist_items → .only). Удаление пункта — delete_checklist_item.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


DELETE_CHECKLIST_ITEM_SPEC = ToolSpec(
    name="delete_checklist_item",
    description=(
        "Удалить пункт жёстко по item_id (item ИСЧЕЗАЕТ; id из "
        "list_checklist_items → ${sN.items.only.item_id}). Триггеры: "
        "«удали пункт X», «не то записала, удали» — в т.ч. когда ТЫ (LLM) "
        "записала не то. Отличается от mark_checklist_item_done (done, ☑) "
        "и archive_checklist."
    ),
    family="checklists",
    effect="write",
    read_domains=["checklists"],
    write_domains=["checklists"],
    input_model=DeleteChecklistItemInput,
    output_model=DeleteChecklistItemOutput,
    trigger_examples=[
        "удали пункт «лаванда 298» из плана кроя",
        "убери из списка по машине — стекло",
        "не то записала, удали колодки",
        "вычеркни пункт про рассаду",
    ],
    mutex_notes=[
        "Удаление ПУНКТА по item_id (из list_checklist_items → .only). «Сделано» — mark_checklist_item_done. Весь список — archive_checklist.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


ARCHIVE_CHECKLIST_SPEC = ToolSpec(
    name="archive_checklist",
    description=(
        "Архивировать ВЕСЬ чек-лист — скрыть из list_checklists и Mini App "
        "(строки остаются в БД). Триггеры: «закрой список X», «убери план "
        "кроя», «архивируй». Возвращает ok:archived:<id> или "
        "error:checklist_list_not_found. Отличается от delete_checklist_item "
        "(удаление ОДНОГО пункта)."
    ),
    family="checklists",
    effect="write",
    # Codex Sub-A4 checklists R1 MINOR #2: runtime fuzzy-resolves
    # list_id_or_title via find_list_by_title BEFORE archiving
    # (housewife_chat_tools.py:2688), so checklists is in read_domains
    # for accurate concurrency tracking.
    read_domains=["checklists"],
    write_domains=["checklists"],
    input_model=ArchiveChecklistInput,
    output_model=ArchiveChecklistOutput,
    trigger_examples=[
        "закрой список план кроя",
        "архивируй чек-лист по машине",
        "убери план дача из активных",
        "архивировать checklist ремонт",
    ],
    mutex_notes=[
        "Архивация ВСЕГО списка. Удаление ОДНОГО пункта — delete_checklist_item. Отметка пункта done — mark_checklist_item_done.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


CHECKLISTS_SPECS: list[ToolSpec] = [
    CREATE_CHECKLIST_SPEC,
    ADD_CHECKLIST_ITEMS_SPEC,
    MOVE_TASK_TO_CHECKLIST_SPEC,
    LIST_CHECKLISTS_SPEC,
    SHOW_CHECKLIST_SPEC,
    LIST_CHECKLIST_ITEMS_SPEC,
    MARK_CHECKLIST_ITEM_DONE_SPEC,
    DELETE_CHECKLIST_ITEM_SPEC,
    ARCHIVE_CHECKLIST_SPEC,
]


__all__ = [
    "ADD_CHECKLIST_ITEMS_SPEC",
    "ARCHIVE_CHECKLIST_SPEC",
    "AddChecklistItemsInput",
    "ArchiveChecklistInput",
    "CHECKLISTS_SPECS",
    "CREATE_CHECKLIST_SPEC",
    "ChecklistItemTitle",
    "ChecklistTitle",
    "CreateChecklistInput",
    "DELETE_CHECKLIST_ITEM_SPEC",
    "DeleteChecklistItemInput",
    "ItemTitleMatch",
    "LIST_CHECKLIST_ITEMS_SPEC",
    "LIST_CHECKLISTS_SPEC",
    "ListChecklistItemsInput",
    "ListChecklistsInput",
    "ListIdOrTitle",
    "MARK_CHECKLIST_ITEM_DONE_SPEC",
    "MOVE_TASK_TO_CHECKLIST_SPEC",
    "MarkChecklistItemDoneInput",
    "MoveTaskToChecklistInput",
    "SHOW_CHECKLIST_SPEC",
    "ShowChecklistInput",
]
