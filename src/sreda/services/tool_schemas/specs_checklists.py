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

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.common import ChecklistId, TaskId
from sreda.services.tool_schemas.housewife import (
    AddChecklistItemsOutput,
    ArchiveChecklistOutput,
    CreateChecklistOutput,
    DeleteChecklistItemOutput,
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


ChecklistItemTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]
"""One item inside a checklist. Wider than title cap because items
can carry more detail («Лаванда 298 ТС, простыня 141×200×19»)."""


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
"""Substring used by mark_checklist_item_done / delete_checklist_item
to fuzzy-locate an item within a list (housewife_chat_tools.py:2624,2663)."""


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
    """Atomic task → checklist move. Cancels source task, dedup-adds
    to target. Cross-family tool (tasks domain → checklists domain)."""

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


class MarkChecklistItemDoneInput(BaseModel):
    """Mark one item as done — runtime fuzzy-matches item by title
    substring within the resolved list."""

    model_config = ConfigDict(extra="forbid")
    list_id_or_title: ListIdOrTitle
    item_title_match: ItemTitleMatch


class DeleteChecklistItemInput(BaseModel):
    """Hard-delete one item — runtime searches across ALL statuses
    (pending/done) since user may want to remove already-completed
    items too (housewife_chat_tools.py:2665 ``only_pending=False``)."""

    model_config = ConfigDict(extra="forbid")
    list_id_or_title: ListIdOrTitle
    item_title_match: ItemTitleMatch


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
        "Используй ТОЛЬКО когда юзер явно «заведи пустой список Y». "
        "В 90% случаев юзер хочет сразу записать список С ПУНКТАМИ — "
        "тогда используй add_checklist_items (он сам создаст список "
        "если такого нет). НЕ для покупок (add_shopping_items) и НЕ "
        "для задач с датой (add_task). Возвращает ok:created:checklist_X:title."
    ),
    family="checklists",
    effect="write",
    read_domains=[],
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
        "ТОЛЬКО для пустого списка. С пунктами сразу — add_checklist_items. Для покупок — add_shopping_items. Для задач с временем — add_task.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


ADD_CHECKLIST_ITEMS_SPEC = ToolSpec(
    name="add_checklist_items",
    description=(
        "Добавить пункты в чек-лист. ВАЖНО: автоматически создаст "
        "новый список с этим title если такого ещё нет — один вызов "
        "вместо create_checklist + add_checklist_items. Триггеры: "
        "«запиши в дела по машине: колодки, стекло, масло», «добавь "
        "в дела на дачу: лопата, рассада», «план кроя на неделю: "
        "лаванда, шампань, жемчуг». Дедуп против существующих "
        "pending items — дубли идут в dups count. Возвращает "
        "ok:added:N:list=<id> или ok:added:N:dups:M:list=<id>."
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
        "Auto-create + populate в одном вызове. Для пустого списка → create_checklist. Для покупок → add_shopping_items. Для задач с датой → add_task.",
        "Дубли — runtime НЕ ошибка, идут в dups count. План не должен ретраить.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


MOVE_TASK_TO_CHECKLIST_SPEC = ToolSpec(
    name="move_task_to_checklist",
    description=(
        "Перенести задачу из Расписания в чек-лист как ПУНКТ — атомарно "
        "(задача сворачивается). ОДИН вызов вместо cancel_task + "
        "add_checklist_items (безопаснее: невозможно потерять task "
        "частично или задвоить). Используй когда юзер: «перенеси X "
        "из расписания в дела/чек-лист Y», «это не на конкретное "
        "время — переложи в дела». В одной транзакции: cancel task "
        "(с reminder если был) + add item с title задачи в target "
        "checklist (с dedup). Target создаётся если не найден. "
        "Возвращает ok:moved:item_id=<clitem>:list=<cid> или "
        "ok:moved:item_id=existing:list=<cid>:dup (если такой пункт "
        "уже был — идемпотентно). Граница vs link_task_to_checklist: "
        "тот СВЯЗЫВАЕТ task ↔ checklist (оба остаются), этот "
        "ПРЕВРАЩАЕТ task В пункт (task cancelled)."
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
        "Превращение task → пункт чек-листа (task cancelled). Для логической СВЯЗИ task ↔ checklist оба остаются — link_task_to_checklist из группы ЗАДАЧИ.",
        "Идемпотентно по item: если пункт уже был, runtime НЕ создаёт дубль (status=moved_dup).",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


LIST_CHECKLISTS_SPEC = ToolSpec(
    name="list_checklists",
    description=(
        "Показать все активные чек-листы юзера со счётчиками pending/done/total. "
        "Используй когда юзер «какие у меня списки», «покажи все мои планы», "
        "«что у меня в чек-листах». Возвращает СТРУКТУРИРОВАННЫЙ список — "
        "переходи к ${list_checklists.checklists[i].checklist_id} для "
        "show_checklist / mark_checklist_item_done / archive_checklist. "
        "Пусто → статус empty (предложи юзеру create_checklist или "
        "add_checklist_items)."
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
        "Используй когда юзер «покажи план кроя», «что осталось в списке X», "
        "«что я ещё не сделал из плана». Поддерживает либо checklist_<id> "
        "либо нечёткий поиск по title. Возвращает СТРУКТУРИРОВАННЫЕ items "
        "с item_id для последующих mark_checklist_item_done / "
        "delete_checklist_item. Список без пунктов → статус empty (но сам "
        "список существует — отличается от not_found)."
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
        "Возвращает ПУНКТЫ одного списка. Для списка ВСЕХ списков — list_checklists.",
        "Используй чтобы получить item_id перед mark_checklist_item_done / delete_checklist_item когда юзер назвал пункт.",
    ],
    timeout_seconds=5,
    side_effect_class="read_only",
)


MARK_CHECKLIST_ITEM_DONE_SPEC = ToolSpec(
    name="mark_checklist_item_done",
    description=(
        "Отметить пункт чек-листа как сделанный. Используй когда юзер: "
        "«сделал X», «купила Y», «закройла лаванду». Runtime fuzzy-matches "
        "item по title substring внутри resolved list. Если list_id_or_title "
        "ещё не известен (юзер назвал по имени) — сначала list_checklists "
        "/ show_checklist для resolve'а. Возвращает ok:done:<item>:title "
        "или error:checklist_list_not_found / error:checklist_item_not_found."
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
        "Только для отметки «сделано». Удаление пункта — delete_checklist_item. Отмена через статус нельзя — для повторного открытия используется отдельная операция (не migrated).",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


DELETE_CHECKLIST_ITEM_SPEC = ToolSpec(
    name="delete_checklist_item",
    description=(
        "Удалить один пункт из чек-листа жёстко (item ИСЧЕЗАЕТ). "
        "Используй когда юзер: «удали пункт X», «убери из списка Y», "
        "«не то записала, удали» — pure correction. Особенно полезно "
        "когда ТЫ (LLM) ошибочно записал не то в прошлом turn'е и юзер "
        "просит убрать неправильный пункт — остаётся только исправленный. "
        "Ищет и по pending И по done items (runtime "
        "only_pending=False). Отличается от mark_checklist_item_done "
        "(status=done, пункт остаётся ☑) и от archive_checklist (весь "
        "список из активных)."
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
        "Только для УДАЛЕНИЯ ПУНКТА. Для «сделано» — mark_checklist_item_done. Для убрать ВЕСЬ список — archive_checklist.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


ARCHIVE_CHECKLIST_SPEC = ToolSpec(
    name="archive_checklist",
    description=(
        "Архивировать ВЕСЬ чек-лист — скрыть из list_checklists и Mini App, "
        "но строки остаются в БД для recall истории. Используй когда юзер: "
        "«закрой список X», «убери план кроя», «архивируй». Возвращает "
        "ok:archived:<id> или error:checklist_list_not_found. Отличается "
        "от delete_checklist_item (удаление ОДНОГО пункта)."
    ),
    family="checklists",
    effect="write",
    read_domains=[],
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
    "LIST_CHECKLISTS_SPEC",
    "ListChecklistsInput",
    "ListIdOrTitle",
    "MARK_CHECKLIST_ITEM_DONE_SPEC",
    "MOVE_TASK_TO_CHECKLIST_SPEC",
    "MarkChecklistItemDoneInput",
    "MoveTaskToChecklistInput",
    "SHOW_CHECKLIST_SPEC",
    "ShowChecklistInput",
]
