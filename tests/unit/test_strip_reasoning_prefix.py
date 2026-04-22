"""Tests for ``strip_reasoning_prefix`` — boundary sanitizer that
removes ReAct-style reasoning markers (``thought\\n``, ``Thinking:``)
from LLM replies before they reach the user. Needed for Gemma-4 tool
mode (verified 2026-04-22) and safe-to-apply on any other model —
idempotent on clean output.
"""

from __future__ import annotations

import pytest

from sreda.services.llm import strip_reasoning_prefix


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Gemma-4 real output format
        ("thought\nВ твоём списке покупок:\n- молоко",
         "В твоём списке покупок:\n- молоко"),
        # Other common variants
        ("Thinking: ok let me answer the question",
         "ok let me answer the question"),
        ("REASONING\n\nfinal answer",
         "final answer"),
        ("analysis:\nthe list is empty",
         "the list is empty"),
        # reflect / reflection
        ("Reflection\n\nГотово, сохранил 3 рецепта.",
         "Готово, сохранил 3 рецепта."),
    ],
)
def test_strips_known_reasoning_markers(raw: str, expected: str) -> None:
    assert strip_reasoning_prefix(raw) == expected


def test_passthrough_for_clean_output() -> None:
    """Normal answer starting with legitimate text must not be touched."""
    clean = "В твоём списке покупок сейчас 3 позиции: молоко, хлеб, яйца."
    assert strip_reasoning_prefix(clean) == clean


def test_idempotent_on_already_clean() -> None:
    """Applying twice equals applying once."""
    raw = "thought\nГотово."
    once = strip_reasoning_prefix(raw)
    twice = strip_reasoning_prefix(once)
    assert once == twice == "Готово."


def test_does_not_strip_legitimate_word_thought_in_sentence() -> None:
    """A reply that genuinely starts with the noun/verb 'thought' in
    a sentence — NOT a ReAct marker — must pass through. The marker
    form is ``thought\\n`` or ``thought:``; prose like 'thought about
    it' doesn't match."""
    prose = "thought about adding tomatoes — should I?"
    # "thought " (space) doesn't match the marker regex, so unchanged.
    assert strip_reasoning_prefix(prose) == prose


def test_empty_and_none_guards() -> None:
    assert strip_reasoning_prefix("") == ""


def test_case_insensitive_match() -> None:
    assert strip_reasoning_prefix("THOUGHT\nok") == "ok"
    assert strip_reasoning_prefix("Thought:\nok") == "ok"


def test_strips_extra_whitespace_after_marker() -> None:
    assert strip_reasoning_prefix("thought\n\n\n  actual answer") == "actual answer"


# ---------------------------------------------------------------------------
# Internal-id scrubber — Grok 4.1 prod 2026-04-22: model dumped
# ``[rec_f5197...]`` after every meal line. User saw tech IDs as
# rendering noise.
# ---------------------------------------------------------------------------


def test_strips_bracketed_recipe_id_after_line() -> None:
    raw = "* Завтрак: Овсянка с ягодами 🥣 [rec_f5197d66b5d44a468bfeb988]"
    cleaned = strip_reasoning_prefix(raw)
    assert "rec_" not in cleaned
    assert "Овсянка с ягодами" in cleaned


def test_strips_recipe_id_without_brackets() -> None:
    raw = "Борщ классический rec_a1b2c3d4e5f6a7b8"
    assert "rec_" not in strip_reasoning_prefix(raw)


def test_strips_menu_plan_and_shopping_ids() -> None:
    raw = "План [menu_abc123def456] — продукт sh_9876543210ab готов"
    cleaned = strip_reasoning_prefix(raw)
    assert "menu_" not in cleaned
    assert "sh_" not in cleaned
    assert "План" in cleaned and "продукт" in cleaned


def test_preserves_prose_that_happens_to_contain_rec_word() -> None:
    """The strip targets only '<prefix>_<hex>' patterns. Prose like
    'я использую recipe_id' or 'record the change' must pass through
    untouched."""
    raw = "Пользователь может использовать recipe_id для поиска — это technical term."
    assert strip_reasoning_prefix(raw) == raw


def test_real_prod_grok_output_cleans_up() -> None:
    raw = (
        "**📅 Расписание еды на завтра (четверг, 23 апреля):**\n\n"
        "* **Завтрак:** Овсянка с ягодами и мёдом 🥣 [rec_f5197d66b5d44a468bfeb988]\n"
        "* **Обед:** Куриный суп с вермишелью 🍲 [rec_213d5c3d718541bfbc597c0e]\n"
        "* **Ужин:** Запечённая курица с картофелем и салатом 🍗 [rec_4dfa7acf86d94c3daeec6ac0]"
    )
    cleaned = strip_reasoning_prefix(raw)
    assert "[rec_" not in cleaned
    # All the dish names still intact
    assert "Овсянка с ягодами и мёдом" in cleaned
    assert "Куриный суп с вермишелью" in cleaned
    assert "Запечённая курица с картофелем и салатом" in cleaned


# ---------------------------------------------------------------------------
# Tool-call syntax leak (Gemma-4 2026-04-22 prod case)
# ---------------------------------------------------------------------------


def test_strips_leading_tool_call_syntax_single_line() -> None:
    """Gemma-4 sometimes opens a reply with the raw tool-call
    signature. The text channel should only contain the human-facing
    reply — strip the echo."""
    raw = "search_recipes(query='Ташкентский плов')\nПрости, рецепт сохранён."
    assert strip_reasoning_prefix(raw) == "Прости, рецепт сохранён."


def test_strips_multi_line_tool_call_syntax() -> None:
    """save_recipe(...) often wraps across many lines because of
    nested ingredients[]. Strip up to the closing paren + newline."""
    raw = (
        "save_recipe(calories_per_serving=650, ingredients=[{'title': 'мясо'}], "
        "title='Плов')\n"
        "Готово! Рецепт сохранён."
    )
    cleaned = strip_reasoning_prefix(raw)
    assert cleaned.startswith("Готово")
    assert "save_recipe" not in cleaned


def test_strips_stack_of_tool_calls() -> None:
    """The real prod leak had TWO tool calls one after another —
    search_recipes + save_recipe — then user-facing text."""
    raw = (
        "search_recipes(query='X')\n"
        "save_recipe(title='X', ingredients=[{}])\n"
        "Прости, техническая заминка — теперь точно сохранено."
    )
    cleaned = strip_reasoning_prefix(raw)
    assert "search_recipes" not in cleaned
    assert "save_recipe" not in cleaned
    assert "Прости" in cleaned


def test_preserves_function_name_in_prose() -> None:
    """Legitimate text that happens to mention a function name in
    the middle of a sentence must not be mangled."""
    raw = "Для проверки я использую search_recipes по ключевому слову."
    assert strip_reasoning_prefix(raw) == raw


def test_preserves_parenthesised_fragments_that_are_not_tool_calls() -> None:
    raw = "Проверил (на всякий случай). Всё ок."
    # The regex requires ``ident(`` at line start, so "(на всякий..."
    # without a leading identifier doesn't match.
    assert strip_reasoning_prefix(raw) == raw


def test_thought_prefix_then_tool_call_strips_both() -> None:
    """Combined case — thought marker on first line, tool-call on
    second, answer after. All meta must go."""
    raw = (
        "thought\n"
        "save_recipe(title='X')\n"
        "Сохранено в книгу."
    )
    assert strip_reasoning_prefix(raw) == "Сохранено в книгу."


# ---------------------------------------------------------------------------
# detect_unbacked_claim — hallucination-without-tool-call guard
# ---------------------------------------------------------------------------


def test_detect_unbacked_claim_fires_on_save_without_tool_call() -> None:
    """Prod 2026-04-22: Gemma wrote 'Я подготовила для тебя рецепт и
    сразу сохранила его в твою книгу рецептов' with tools=[] —
    exactly the case we want to catch."""
    from sreda.services.llm import detect_unbacked_claim

    text = "Я подготовила рецепт и сразу сохранила его в твою книгу рецептов."
    assert detect_unbacked_claim(text, called_tools=set()) is True


def test_detect_unbacked_claim_silent_when_save_tool_was_actually_called() -> None:
    from sreda.services.llm import detect_unbacked_claim

    text = "Готово! Сохранила рецепт в книгу."
    called = {"save_recipe"}
    assert detect_unbacked_claim(text, called_tools=called) is False


def test_detect_unbacked_claim_silent_on_benign_phrases() -> None:
    """Must not false-fire on phrases that use claim verbs but don't
    actually promise a side-effect."""
    from sreda.services.llm import detect_unbacked_claim

    # "сохраню" is future tense → not a claim of done work
    assert detect_unbacked_claim(
        "Я сохраню это на будущее, потом разберёмся.",
        called_tools=set(),
    ) is False

    # "сохранила в памяти" - no rel. object in window
    assert detect_unbacked_claim(
        "Сохранила для следующего раза.",
        called_tools=set(),
    ) is False


def test_detect_unbacked_claim_fires_on_shopping_add() -> None:
    from sreda.services.llm import detect_unbacked_claim

    text = "Добавила молоко в список покупок."
    assert detect_unbacked_claim(text, called_tools=set()) is True
    # Backed by the right tool → silent
    assert detect_unbacked_claim(
        text, called_tools={"add_shopping_items"},
    ) is False


def test_detect_unbacked_claim_fires_on_menu_create() -> None:
    from sreda.services.llm import detect_unbacked_claim

    text = "Создала меню на неделю с учётом твоих предпочтений."
    assert detect_unbacked_claim(text, called_tools=set()) is True
    assert detect_unbacked_claim(
        text, called_tools={"plan_week_menu"},
    ) is False
