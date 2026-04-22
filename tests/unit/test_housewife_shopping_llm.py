"""Tests for the LLM-powered shopping-list transformer — converts raw
recipe ingredients ("6 стаканов молока", "соль по вкусу", "4 ст.л.
мёда") into what you actually BUY at the store ("1,5 л молока", "200 г
мёда", and skips "соль по вкусу" because you don't buy it that way)."""

from __future__ import annotations

import pytest

from sreda.services.housewife_menu import AggregatedIngredient


class _FakeLLM:
    """Minimal langchain ChatModel stand-in that returns a fixed content
    on .invoke(). Enough for the shopping transformer which only does
    one call + JSON-parses the response."""

    def __init__(self, content: str):
        self._content = content

    def invoke(self, messages):
        class _Resp:
            def __init__(self, c):
                self.content = c
        return _Resp(self._content)


# Common ingredient fixture — what the menu aggregator would hand us.
@pytest.fixture
def recipe_ingredients():
    return [
        AggregatedIngredient(
            title="молоко", quantity_text="6 стаканов",
            is_optional=False, source_recipe_id="rec_1",
        ),
        AggregatedIngredient(
            title="мёд", quantity_text="4 ст.л.",
            is_optional=False, source_recipe_id="rec_2",
        ),
        AggregatedIngredient(
            title="соль", quantity_text="по вкусу",
            is_optional=False, source_recipe_id="rec_3",
        ),
        AggregatedIngredient(
            title="курица", quantity_text="500 г",
            is_optional=False, source_recipe_id="rec_4",
        ),
    ]


def test_transformer_produces_shoppable_units(recipe_ingredients):
    """LLM should convert cooking units into buyable ones."""
    from sreda.services.housewife_shopping_llm import (
        convert_ingredients_to_shopping_list,
    )
    fake = _FakeLLM(
        '{"items": ['
        '{"title": "молоко", "quantity_text": "1,5 л", "category": "молочные"},'
        '{"title": "мёд", "quantity_text": "200 г", "category": "бакалея"},'
        '{"title": "курица", "quantity_text": "500 г", "category": "мясо_рыба"}'
        ']}'
    )
    result = convert_ingredients_to_shopping_list(
        recipe_ingredients, eaters_count=3, llm=fake,
    )
    titles = {r["title"] for r in result}
    # "соль по вкусу" should be dropped — you don't buy salt by taste.
    assert "соль" not in titles
    assert titles == {"молоко", "мёд", "курица"}
    by_title = {r["title"]: r for r in result}
    assert by_title["молоко"]["quantity_text"] == "1,5 л"
    assert by_title["мёд"]["quantity_text"] == "200 г"


def test_transformer_skips_llm_for_short_input():
    """Perf: for a tiny input (1-2 items) the aggregation/normalisation
    value of going through the LLM is low — we'd burn a 3-8 second
    round-trip to rename "молоко 500 мл" to "молоко 500 мл". Short-
    circuit to passthrough with category guess.

    An llm that RAISES on .invoke() proves we didn't touch it."""
    from sreda.services.housewife_shopping_llm import (
        convert_ingredients_to_shopping_list,
    )

    class _ExplodingLLM:
        def invoke(self, messages):
            raise AssertionError(
                "LLM should not be called for <=2 ingredients"
            )

    result = convert_ingredients_to_shopping_list(
        [
            AggregatedIngredient(
                title="молоко", quantity_text="1 л",
                is_optional=False, source_recipe_id="rec_1",
            ),
            AggregatedIngredient(
                title="хлеб", quantity_text="1 буханка",
                is_optional=False, source_recipe_id="rec_2",
            ),
        ],
        eaters_count=2,
        llm=_ExplodingLLM(),
    )
    # Passthrough preserves content verbatim
    assert len(result) == 2
    by_title = {r["title"]: r for r in result}
    assert by_title["молоко"]["quantity_text"] == "1 л"
    assert by_title["хлеб"]["quantity_text"] == "1 буханка"
    # Category guess fills from _guess_category dict
    assert by_title["молоко"]["category"] == "молочные"
    assert by_title["хлеб"]["category"] == "хлеб"


def test_transformer_threshold_kicks_in_at_3_items():
    """3 ingredients IS enough to benefit from aggregation/conversion —
    LLM should be invoked."""
    from sreda.services.housewife_shopping_llm import (
        convert_ingredients_to_shopping_list,
    )
    called = {"n": 0}

    class _CountingLLM:
        def invoke(self, messages):
            called["n"] += 1

            class R:
                content = '{"items": [{"title": "x", "quantity_text": "1 шт", "category": "другое"}]}'
            return R()

    convert_ingredients_to_shopping_list(
        [
            AggregatedIngredient(
                title=t, quantity_text=None,
                is_optional=False, source_recipe_id="rec_z",
            )
            for t in ("a", "b", "c")
        ],
        eaters_count=1,
        llm=_CountingLLM(),
    )
    assert called["n"] == 1


def test_transformer_survives_json_wrapped_in_markdown():
    """Some models wrap JSON in ```json code fences or prefix text.
    Extractor must still find the JSON."""
    from sreda.services.housewife_shopping_llm import (
        convert_ingredients_to_shopping_list,
    )
    fake = _FakeLLM(
        "Вот список покупок:\n```json\n"
        '{"items": [{"title": "хлеб", "quantity_text": "1 шт", "category": "хлеб"}]}'
        "\n```\nГотово."
    )
    result = convert_ingredients_to_shopping_list(
        [
            AggregatedIngredient(
                title="хлеб", quantity_text="1 шт",
                is_optional=False, source_recipe_id="rec_x",
            ),
        ],
        eaters_count=1,
        llm=fake,
    )
    assert len(result) == 1
    assert result[0]["title"] == "хлеб"


def test_transformer_empty_input_returns_empty():
    """Don't call LLM for empty input — short-circuit to []."""
    from sreda.services.housewife_shopping_llm import (
        convert_ingredients_to_shopping_list,
    )
    # Passing None for llm — function must not touch it.
    assert convert_ingredients_to_shopping_list([], eaters_count=3, llm=None) == []


def test_transformer_preserves_source_recipe_id_when_single_source(
    recipe_ingredients,
):
    """When an output item corresponds to a single recipe ingredient,
    carry through the source_recipe_id so "купил для борща" UX works."""
    from sreda.services.housewife_shopping_llm import (
        convert_ingredients_to_shopping_list,
    )
    fake = _FakeLLM(
        '{"items": ['
        '{"title": "курица", "quantity_text": "500 г", "category": "мясо_рыба"}'
        ']}'
    )
    result = convert_ingredients_to_shopping_list(
        [recipe_ingredients[3]],  # only курица
        eaters_count=1,
        llm=fake,
    )
    assert result[0].get("source_recipe_id") == "rec_4"


def test_transformer_returns_empty_on_llm_error(recipe_ingredients):
    """If the LLM throws, we don't crash the shopping flow — return
    empty so the endpoint can at least tell the user nothing was
    added."""
    from sreda.services.housewife_shopping_llm import (
        convert_ingredients_to_shopping_list,
    )

    class _BoomLLM:
        def invoke(self, messages):
            raise RuntimeError("llm down")

    result = convert_ingredients_to_shopping_list(
        recipe_ingredients, eaters_count=2, llm=_BoomLLM(),
    )
    assert result == []


def test_transformer_returns_empty_on_malformed_json(recipe_ingredients):
    """LLM returns text without a JSON object — transformer logs and
    returns empty, same as llm error."""
    from sreda.services.housewife_shopping_llm import (
        convert_ingredients_to_shopping_list,
    )
    fake = _FakeLLM("Вот список, но без JSON 😅")
    assert convert_ingredients_to_shopping_list(
        recipe_ingredients, eaters_count=2, llm=fake,
    ) == []
