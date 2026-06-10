"""ToolSpec instances for the SHOPPING family (Sub-A4, item #6 in
the architecture plan).

Each tool gets:
- ``input_model`` (pydantic, ``extra='forbid'``) — strict planner
  contract: exact keyword args the LLM may emit.
- ``output_model`` — discriminated union from ``housewife.py`` parser
  layer.
- ``trigger_examples`` — 3..10 Russian phrases for prompt few-shot.
- ``mutex_notes`` — disambiguation against close-sibling tools
  (mark vs remove, update-item vs update-category).
- ``family`` / ``effect`` / ``read_domains`` / ``write_domains`` —
  scheduler/router metadata.

The python function implementations stay in
``housewife_chat_tools.py`` and produce ``str`` returns; the
``ToolSpec.output_model`` + matching parser in ``housewife.py``
typed-decode those strings for the executor.

Production policy validation happens via
``assert_production_registry_quality(SHOPPING_SPECS)`` — Sub-A4 CI
calls that to fail builds that ship incomplete shopping ToolSpecs.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.common import (
    AddQuantityText,
    CategoryName,
    QuantityText,
    ShoppingItemId,
    ShoppingTitle,
)
from sreda.services.tool_schemas.housewife import (
    AddShoppingItemsOutput,
    ClearBoughtShoppingOutput,
    ListShoppingOutput,
    MarkShoppingBoughtOutput,
    RemoveShoppingItemsOutput,
    UpdateShoppingItemOutput,
    UpdateShoppingItemsCategoryOutput,
)


# ---------------------------------------------------------------------------
# Input models — strict planner contracts (extra='forbid' so the planner
# can't sneak unknown args). Field names match the housewife_chat_tools.py
# kwargs.
# ---------------------------------------------------------------------------


class ShoppingItemInput(BaseModel):
    """One row of an ``add_shopping_items`` batch.

    Field types:
    - ``title``: ``ShoppingTitle`` (non-blank, ≤500) — matches runtime
      cap (``housewife_shopping.py:252``)
    - ``quantity_text``: ``AddQuantityText`` (non-blank, ≤64) — matches
      runtime cap (``housewife_shopping.py:253``)
    - ``category``: ``CategoryName`` (non-blank, ≤64) — matches runtime
      ``_normalize_category`` (``housewife_shopping.py:96``)

    Codex R3 MAJOR #1: ``quantity_text`` was previously typed as
    ``ShoppingTitle | None`` (500-char cap advertised in JSON schema)
    with a ``model_validator`` enforcing 64. Field-level
    ``AddQuantityText`` (≤64) makes the cap visible in JSON schema and
    enforced even when the planner's refs-present validation path
    skips model_validators.
    """

    model_config = ConfigDict(extra="forbid")
    title: ShoppingTitle
    quantity_text: AddQuantityText | None = None
    category: CategoryName | None = None


class AddShoppingItemsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ShoppingItemInput] = Field(min_length=1)


class MarkShoppingBoughtInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[ShoppingItemId] = Field(min_length=1)


class RemoveShoppingItemsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[ShoppingItemId] = Field(min_length=1)


class UpdateShoppingItemInput(BaseModel):
    """Update a single shopping item in place.

    Codex R1 MAJOR #2: the original schema accepted
    ``{"item_id": "sh_42"}`` with all three mutable fields missing
    as a valid no-op call. That wastes a tool round-trip and produces
    a misleading ``ok:updated:sh_42`` for «nothing changed». Reject
    via ``model_validator``.

    Codex R2 MAJOR #3: ``quantity_text=""`` is a legitimate intent
    («убери количество у молока») — runtime maps it to ``None``
    (clear quantity) at ``housewife_shopping.py:401-402``. Switch the
    no-op check from «all None» to «no fields were sent» via
    ``model_fields_set`` so ``quantity_text=""`` passes through as a
    real update.

    Field-level types:
    - ``title`` (when present): non-blank ≤500 (runtime caps to 500
      and silently no-ops empty at lines 396-399)
    - ``quantity_text`` (when present): ≤64 (allows empty string to
      clear — that's the whole point of this field on update)
    - ``category`` (when present): non-blank ≤64 (runtime
      ``_coerce_category`` rejects blank, caps to 64)
    """

    model_config = ConfigDict(extra="forbid")
    item_id: ShoppingItemId
    title: ShoppingTitle | None = None
    quantity_text: QuantityText | None = None
    category: CategoryName | None = None

    @model_validator(mode="after")
    def _at_least_one_mutable_field(self) -> "UpdateShoppingItemInput":
        # Rule: a field is «provided» if it's in ``model_fields_set``
        # AND its value is not ``None``. Empty string for
        # ``quantity_text`` counts as provided (clear intent). At least
        # one mutable field must be provided — otherwise the runtime
        # ends up with `if X is not None` skipping every branch and
        # the call is a silent no-op.
        #
        # Codex R3 MAJOR #2: R2's plain ``model_fields_set`` check let
        # ``{"item_id": ..., "title": None}`` through (explicit null
        # appears in model_fields_set). Runtime line 396-397
        # short-circuits on ``title is None``, so the call became a
        # misleading ``ok:updated:<id>`` with no actual mutation.
        def _is_provided(name: str) -> bool:
            if name not in self.model_fields_set:
                return False
            value = getattr(self, name)
            # ``quantity_text=""`` is the «clear» intent — runtime
            # (housewife_shopping.py:401-402) maps it to None on the
            # row. Empty string IS not None, so it counts as provided
            # naturally — no special-case needed.
            return value is not None

        provided = [n for n in ("title", "quantity_text", "category") if _is_provided(n)]
        if not provided:
            raise ValueError(
                "update_shopping_item requires at least one of "
                "title / quantity_text / category with a non-null "
                "value — explicit nulls and missing fields both "
                "produce a silent no-op call."
            )
        return self


class UpdateShoppingItemsCategoryInput(BaseModel):
    """Codex R1 MINOR #8: bulk re-category needs ≥2 item_ids — a
    single-id reassignment should use ``update_shopping_item`` (which
    surfaces the more-specific ``ok:updated:<id>`` return).

    Codex R2 MAJOR #2: ``category`` uses ``CategoryName`` (≤64) to
    match runtime ``_coerce_category`` behaviour — was ``NonBlankStr``
    (uncapped), which would let the planner submit a 1000-char string
    that runtime silently truncates to 64.
    """

    model_config = ConfigDict(extra="forbid")
    item_ids: list[ShoppingItemId] = Field(min_length=2)
    category: CategoryName


class ListShoppingInput(BaseModel):
    """Reads the current pending list for the calling tenant.

    #122: ``title_match`` — подстрока названия (регистр/ё неважны) для
    «удали/отметь X по имени»: список возвращается уже отфильтрованным,
    дальше ``${sN.items.only.item_id}`` берёт ровно-один."""
    model_config = ConfigDict(extra="forbid")
    title_match: str | None = None


class ClearBoughtShoppingInput(BaseModel):
    """No args — bulk housekeeping for the calling tenant."""
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# ToolSpec instances — passes assert_production_registry_quality.
# ---------------------------------------------------------------------------


ADD_SHOPPING_ITEMS_SPEC = ToolSpec(
    name="add_shopping_items",
    description=(
        "Добавить продукты в список покупок одним батчем. Используй для "
        "запросов вида «купи X», «добавь Y в покупки», «надо купить Z». "
        "Только title обязателен, остальное опционально."
    ),
    family="shopping",
    effect="write",
    read_domains=[],
    write_domains=["shopping"],
    input_model=AddShoppingItemsInput,
    output_model=AddShoppingItemsOutput,
    # #29: "empty" (count=0) is an idempotent no-op — nothing written.
    # #115: "replay" means the rows EXIST — an earlier retry of this same step
    # inserted them (ON CONFLICT on the per-item operation_id). The durable
    # write is committed from recovery's point of view, just not by this call.
    committed_statuses=frozenset({"added", "replay"}),
    trigger_examples=[
        "купи молоко и хлеб",
        "добавь в покупки яйца",
        "надо купить картошки 1 кг",
        "запиши в список огурцы и помидоры",
        "ещё творог тоже",
    ],
    mutex_notes=[
        "Используй ТОЛЬКО для добавления новых пунктов. Чтобы отметить уже купленное — mark_shopping_bought, а не add.",
        "Для удаления / отмены — remove_shopping_items, не add с минусом.",
        "Для редактирования существующей строки — update_shopping_item, а не add+remove.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
    # Sub-A12 Phase E PR-2a: option-(a) idempotency adapter wired in
    # HousewifeShoppingService.add_items (housewife_shopping.py) — writes
    # operation_id + normalized_title_hash + ON CONFLICT DO NOTHING + emit_event
    # atomically when running under the planner (ctx is not None).
    has_idempotency_adapter=True,
)


MARK_SHOPPING_BOUGHT_SPEC = ToolSpec(
    name="mark_shopping_bought",
    description=(
        "Отметить пункты в списке покупок как купленные. Они уходят из "
        "обычного вида в bought-статус. Применяй когда юзер сказал что "
        "что-то уже купил."
    ),
    family="shopping",
    effect="write",
    read_domains=[],
    write_domains=["shopping"],
    input_model=MarkShoppingBoughtInput,
    output_model=MarkShoppingBoughtOutput,
    trigger_examples=[
        "молоко купил",
        "отметь хлеб как купленный",
        "купил молоко и яйца",
        "хлеб и творог куплены",
    ],
    mutex_notes=[
        "Используй ТОЛЬКО для купленного (требует конкретные item_ids из list_shopping). Для удаления / отмены (не покупал) — remove_shopping_items.",
        "Для «купил всё из списка» — сначала list_shopping, потом mark_shopping_bought с полученными ids.",
        "Не вызывай add_shopping_items + mark — для batch-добавления-и-сразу-отметки сделай две отдельные операции последовательно.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


REMOVE_SHOPPING_ITEMS_SPEC = ToolSpec(
    name="remove_shopping_items",
    description=(
        "Убрать пункты из списка покупок без отметки «куплено». "
        "Используй когда юзер сказал «не надо», «перехотел», «убери из "
        "списка». Отличается от mark_shopping_bought — товар не был "
        "куплен, он отменён."
    ),
    family="shopping",
    effect="write",
    read_domains=[],
    write_domains=["shopping"],
    input_model=RemoveShoppingItemsInput,
    output_model=RemoveShoppingItemsOutput,
    trigger_examples=[
        "убери молоко из списка",
        "не надо хлеб",
        "перехотел творог",
        "вычеркни яйца",
    ],
    mutex_notes=[
        "Используй ТОЛЬКО для отмены. Для уже купленного — mark_shopping_bought, не remove.",
        "Для замены товара (молоко на кефир) — update_shopping_item, не remove+add.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


UPDATE_SHOPPING_ITEM_SPEC = ToolSpec(
    name="update_shopping_item",
    description=(
        "Обновить одну строку в списке покупок — переименовать, изменить "
        "количество или категорию БЕЗ remove+add цикла. Любой параметр "
        "None оставляет поле без изменения."
    ),
    family="shopping",
    effect="write",
    read_domains=[],
    write_domains=["shopping"],
    input_model=UpdateShoppingItemInput,
    output_model=UpdateShoppingItemOutput,
    trigger_examples=[
        "поменяй молоко на кефир",
        "вместо одной бутылки две",
        "молоко в категорию молочные",
        "переименуй хлеб в батон",
    ],
    mutex_notes=[
        "Используй для редактирования ОДНОЙ строки. Для bulk-категоризации — update_shopping_items_category.",
        "НЕ вызывай remove + add — это две LLM-итерации и 5-10 секунд лишней латенси.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
    # Codex Sub-A4 R4 MAJOR #1: the input_model's @model_validator catches
    # the no-op fully-literal case, but the planner's refs-present
    # validation path skips model_validators. Declaring the names here
    # gates the planner's static validator on top of the model layer:
    # at least one mutable field must be present and non-null (refs OK,
    # empty string OK for the «clear» intent on quantity_text).
    required_any_non_null_args=["title", "quantity_text", "category"],
)


UPDATE_SHOPPING_ITEMS_CATEGORY_SPEC = ToolSpec(
    name="update_shopping_items_category",
    description=(
        "Поменять категорию у нескольких пунктов одним батчем. Используй "
        "когда юзер просит перегруппировать список («все молочные в одну "
        "категорию», «лекарства отдельно»). Один tool-call вместо цикла."
    ),
    family="shopping",
    effect="write",
    read_domains=[],
    write_domains=["shopping"],
    input_model=UpdateShoppingItemsCategoryInput,
    output_model=UpdateShoppingItemsCategoryOutput,
    trigger_examples=[
        "сгруппируй молочные продукты",
        "лекарства в отдельную категорию",
        "перенеси яйца и сыр в молочные",
        "помидоры и огурцы в овощи",
    ],
    mutex_notes=[
        "Используй для BULK перекатегоризации. Для одной строки — update_shopping_item.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


LIST_SHOPPING_SPEC = ToolSpec(
    name="list_shopping",
    description=(
        "Показать текущий pending-список покупок юзера, сгруппированный "
        "по категориям. Использовать когда юзер спрашивает «что в "
        "списке?», «что покупать?», «сколько ещё осталось?». #122: для "
        "«удали/отметь X по имени» передавай title_match=<подстрока "
        "названия, регистр/ё неважны> — вернутся только совпадающие, "
        "дальше бери ровно-один через ${sN.items.only.item_id} (в списковых "
        "аргументах оборачивай: [\"${...}\"]). filter()/where() в ссылках "
        "НЕ существует."
    ),
    family="shopping",
    effect="read",
    read_domains=["shopping"],
    write_domains=[],
    input_model=ListShoppingInput,
    output_model=ListShoppingOutput,
    trigger_examples=[
        "что в списке покупок?",
        "что мне покупать",
        "покажи список",
        "сколько ещё осталось купить",
    ],
    mutex_notes=[],
    timeout_seconds=5,
    side_effect_class="read_only",
)


CLEAR_BOUGHT_SHOPPING_SPEC = ToolSpec(
    name="clear_bought_shopping",
    description=(
        "Архивировать все уже-купленные пункты — bulk housekeeping после "
        "похода в магазин. Не трогает pending-строки, только убирает "
        "bought из истории."
    ),
    family="shopping",
    effect="write",
    read_domains=[],
    write_domains=["shopping"],
    input_model=ClearBoughtShoppingInput,
    output_model=ClearBoughtShoppingOutput,
    trigger_examples=[
        "очисти купленные",
        "убери уже купленное",
        "почисти историю покупок",
        "убери купленное из истории",
    ],
    mutex_notes=[
        "Используй ТОЛЬКО для bought-строк (история). Для отмены pending-строк (текущий список) — remove_shopping_items.",
        "Не вызывай при запросе «очисти весь список» без уточнения у юзера — есть риск удалить непрочитанные pending; уточни сначала.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


SHOPPING_SPECS: list[ToolSpec] = [
    ADD_SHOPPING_ITEMS_SPEC,
    MARK_SHOPPING_BOUGHT_SPEC,
    REMOVE_SHOPPING_ITEMS_SPEC,
    UPDATE_SHOPPING_ITEM_SPEC,
    UPDATE_SHOPPING_ITEMS_CATEGORY_SPEC,
    LIST_SHOPPING_SPEC,
    CLEAR_BOUGHT_SHOPPING_SPEC,
]
"""All 7 shopping ToolSpec instances in stable order. Sub-A4 CI tests
construct this list and call ``assert_production_registry_quality``
on it to enforce the production-policy gate."""


__all__ = [
    "ADD_SHOPPING_ITEMS_SPEC",
    "AddShoppingItemsInput",
    "CLEAR_BOUGHT_SHOPPING_SPEC",
    "ClearBoughtShoppingInput",
    "LIST_SHOPPING_SPEC",
    "ListShoppingInput",
    "MARK_SHOPPING_BOUGHT_SPEC",
    "MarkShoppingBoughtInput",
    "REMOVE_SHOPPING_ITEMS_SPEC",
    "RemoveShoppingItemsInput",
    "SHOPPING_SPECS",
    "ShoppingItemInput",
    "UPDATE_SHOPPING_ITEM_SPEC",
    "UPDATE_SHOPPING_ITEMS_CATEGORY_SPEC",
    "UpdateShoppingItemInput",
    "UpdateShoppingItemsCategoryInput",
]
