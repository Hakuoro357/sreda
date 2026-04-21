"""LLM-powered shopping-list transformer.

Recipes store ingredients in COOKING units ("6 стаканов молока",
"соль по вкусу", "4 ст.л. мёда"). A shopping list needs BUYABLE units
(litres, kilograms, packets). When we naively copy recipe ingredients
to the shopping list (generate_shopping_from_menu / per-day button /
regen-sync), the result is a garbage list — "молоко 6 стаканов", "соль
по вкусу". Users tried to literally buy "соль по вкусу" and gave up.

This module converts a batch of :class:`AggregatedIngredient` rows
into a ``list[dict]`` ready for ``HousewifeShoppingService.add_items``.
Handles:

* Unit conversion — стаканы → литры, ложки → граммы, ml → л/мл.
* Aggregation — "молоко из рецепта А (500мл)" + "молоко из рецепта Б
  (1л)" → "молоко 1,5 л".
* Drop-list — "соль по вкусу", "перец по вкусу", "вода для варки" get
  dropped (user has them on hand or doesn't buy by amount).
* Category mapping — reuses the fixed taxonomy
  (``SHOPPING_CATEGORIES``).

Design notes:

* The LLM call is injectable (``llm`` kwarg) so tests can stub it
  without touching network or OpenAI keys. Production path defaults to
  ``get_chat_llm()`` — a lightweight call, no tool loop, one JSON
  response parsed with a permissive regex.
* Any failure (LLM down, non-JSON response, malformed shape) returns
  ``[]`` so the caller can surface "ничего не добавлено" instead of
  crashing the shopping flow. We log a warning for observability.
* When the input contains a single source_recipe_id we preserve it on
  the output so "купил для Борща" UX stays intact. Multi-source
  aggregates get source_recipe_id=None (the item belongs to multiple
  dishes, no single anchor).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sreda.services.housewife_menu import AggregatedIngredient

logger = logging.getLogger(__name__)


def convert_ingredients_to_shopping_list(
    ingredients: list[AggregatedIngredient],
    *,
    eaters_count: int,
    llm: Any = None,
) -> list[dict[str, Any]]:
    """Transform raw recipe ingredients into a buyable shopping list.

    Args:
        ingredients: list of AggregatedIngredient from menu aggregation.
        eaters_count: family size (already baked into the per-ingredient
            quantities by the aggregator, but we pass it to the LLM too
            as a sanity anchor).
        llm: optional injected LangChain-compatible ChatModel. ``None``
            uses ``get_chat_llm()``. In tests pass a stub with a fake
            ``.invoke()``.

    Returns:
        list of ``{title, quantity_text, category, source_recipe_id?}``
        dicts ready for ``HousewifeShoppingService.add_items``. Empty
        list on any failure (caller decides how to surface).
    """
    if not ingredients:
        return []

    if llm is None:
        from sreda.services.llm import get_chat_llm

        llm = get_chat_llm()
        if llm is None:
            logger.warning(
                "shopping transformer: LLM not configured, returning empty"
            )
            return []

    # Import here to avoid langchain import cost at module load.
    from langchain_core.messages import HumanMessage, SystemMessage

    source_lines: list[str] = []
    for ing in ingredients:
        qty = ing.quantity_text or "сколько нужно"
        source_lines.append(f"- {ing.title}: {qty}")
    source_blob = "\n".join(source_lines)

    prompt = (
        "Тебе нужно составить РЕАЛЬНЫЙ список покупок из "
        f"ингредиентов {len(ingredients)} рецептов (для {eaters_count} едоков). "
        "Ингредиенты ниже — это то что написано В РЕЦЕПТАХ, то есть "
        "кулинарные единицы (стаканы, ложки, 'по вкусу'). "
        "Тебе нужно превратить их в то, что реально ПОКУПАЮТ в магазине.\n\n"
        "Правила:\n"
        "1. Переведи кулинарные единицы в магазинные: "
        "стаканы→литры (1 стакан ≈ 250мл), "
        "ст.л.→граммы (1 ст.л. ≈ 15г), ч.л.→граммы, "
        "штуки оставляй как есть.\n"
        "2. Объедини одинаковые продукты из разных рецептов в один, суммируя количества.\n"
        "3. УДАЛИ позиции которые не покупают: 'соль по вкусу', 'перец по вкусу', "
        "'вода для варки', 'масло для жарки' (в разумных количествах). "
        "У пользователя это уже есть дома.\n"
        "4. Категории: молочные / мясо_рыба / овощи_фрукты / хлеб / "
        "бакалея / напитки / готовое / замороженное / бытовая_химия / "
        "другое. Выбери подходящую.\n"
        "5. Округляй до торговых фасовок: '1,5 л', '500 г', '1 пачка', '10 шт'. "
        "Избегай дробей типа '0,875 л'.\n\n"
        f"Исходный список (на {eaters_count} едоков):\n{source_blob}\n\n"
        "Верни СТРОГО JSON без комментариев:\n"
        '{"items": ['
        '{"title": "молоко", "quantity_text": "1,5 л", "category": "молочные"}, '
        "..."
        "]}"
    )

    try:
        resp = llm.invoke([
            SystemMessage(
                content=(
                    "Ты составляешь список покупок. Отвечай строго в JSON."
                )
            ),
            HumanMessage(content=prompt),
        ])
    except Exception:  # noqa: BLE001
        logger.exception("shopping transformer: LLM invoke failed")
        return []

    raw = (getattr(resp, "content", "") or "").strip()
    match = re.search(r"\{[\s\S]*\}", raw)
    if match is None:
        logger.warning(
            "shopping transformer: no JSON object in LLM response: %r",
            raw[:200],
        )
        return []
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, TypeError):
        logger.warning(
            "shopping transformer: malformed JSON: %r",
            match.group(0)[:200],
        )
        return []

    if not isinstance(parsed, dict):
        return []
    items = parsed.get("items")
    if not isinstance(items, list):
        return []

    # Propagate source_recipe_id only when every input ingredient
    # shares one — tells us the caller was processing a single-recipe
    # batch (e.g. regen-sync for one cell).
    unique_sources = {
        ing.source_recipe_id for ing in ingredients if ing.source_recipe_id
    }
    single_source = (
        next(iter(unique_sources)) if len(unique_sources) == 1 else None
    )

    out: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        qty = (entry.get("quantity_text") or "").strip() or None
        cat = (entry.get("category") or "").strip() or None
        row: dict[str, Any] = {
            "title": title,
            "quantity_text": qty,
            "category": cat,
        }
        if single_source:
            row["source_recipe_id"] = single_source
        out.append(row)
    return out
