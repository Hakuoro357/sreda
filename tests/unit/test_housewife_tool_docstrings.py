"""Lock-in tests for LLM-tool descriptions and the conversation system
prompt. These aren't "runtime" tests — they guard against someone
silently removing critical instructions in a future refactor. The
phrases checked here shape LLM behaviour in ways we observed on prod
(otherwise recipes skip heat level, family gets duplicated, recipes
get double-saved after tool-budget restarts, etc).

Each test should fail loudly with a message that explains WHICH prompt
nudges is missing and WHY it matters, so a future developer who hits
the failure understands the regression risk.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.services.housewife_chat_tools import build_housewife_tools


def _tools():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.commit()
    return {
        t.name: t
        for t in build_housewife_tools(
            session=sess, tenant_id="t1", user_id="u1"
        )
    }


# ---------------------------------------------------------------------------
# Heat level rule — every save_recipe / save_recipes_batch /
# plan_week_menu docstring must instruct the LLM to specify fire
# intensity for each cooking step (observed prod feedback: LLM often
# wrote "варить 5 минут" without saying at what heat).
# ---------------------------------------------------------------------------


def test_save_recipe_docstring_requires_heat_level_in_steps():
    tools = _tools()
    desc = tools["save_recipe"].description
    assert "огне" in desc.lower(), (
        "save_recipe docstring must instruct the LLM to specify "
        "heat intensity ('огне') for each cooking step. Users complain "
        "when recipe steps just say 'варить 5 минут' without saying at "
        "what heat level to cook."
    )


def test_save_recipes_batch_docstring_requires_heat_level():
    tools = _tools()
    desc = tools["save_recipes_batch"].description
    assert "огне" in desc.lower(), (
        "save_recipes_batch must also instruct the LLM about heat "
        "level in instructions_md — bulk-save path is the most common "
        "entry for 'сохрани 18 рецептов из книги' flows."
    )


def test_plan_week_menu_docstring_mentions_heat_for_free_text():
    tools = _tools()
    desc = tools["plan_week_menu"].description
    assert "огне" in desc.lower() or "температур" in desc.lower(), (
        "plan_week_menu free_text cells often describe cooking method "
        "briefly. Docstring should nudge the LLM to include heat level "
        "or oven temperature inline when describing cooking steps."
    )


# ---------------------------------------------------------------------------
# Conversation system prompt — a shorter reminder lives here so the
# rule stays active even when individual tool docstrings scroll out of
# the model's attention.
# ---------------------------------------------------------------------------


def test_system_prompt_has_heat_level_rule():
    from sreda.runtime.handlers import _CONVERSATION_SYSTEM_PROMPT

    assert "огне" in _CONVERSATION_SYSTEM_PROMPT.lower(), (
        "_CONVERSATION_SYSTEM_PROMPT must mention heat intensity so "
        "the rule stays sticky across multi-turn conversations. Tool "
        "docstrings alone aren't enough — they get compressed when "
        "the model's context window fills up."
    )


# ---------------------------------------------------------------------------
# Stage 7.5 — anti-hallucination, book-vs-menu split, plan_week_menu
# overwrite warning, and "state via tools not memory" rules.
# ---------------------------------------------------------------------------


def test_system_prompt_forbids_memory_for_shopping_state():
    """Bug observed 2026-04-22 tenant_tg_755682022: user marked items
    bought in Mini App; next day agent listed them as still pending.
    Service filter is correct. Likely cause: LLM pulls old shopping
    state from [ПАМЯТЬ] instead of calling list_shopping(). Explicit
    rule in system prompt required."""
    from sreda.runtime.handlers import _CONVERSATION_SYSTEM_PROMPT

    low = _CONVERSATION_SYSTEM_PROMPT.lower()
    # Need to explicitly say: for shopping/recipes/menu → use tools,
    # not memory. Accept several wordings.
    has_rule = any(
        phrase in low
        for phrase in (
            "не по памяти", "не из памяти", "не по [память",
            "состояние.*через tool", "источник правды",
        )
    )
    # Also require mention of list_shopping specifically
    assert "list_shopping" in _CONVERSATION_SYSTEM_PROMPT and has_rule, (
        "System prompt must instruct LLM that shopping/menu/recipe "
        "STATE comes from tools (list_shopping, list_menu, "
        "search_recipes), NOT from [ПАМЯТЬ]. Memory is for long-term "
        "profile facts (family composition, allergies) — not mutable "
        "state that changes between turns."
    )


def test_system_prompt_anti_hallucination_tool_call_rule():
    """LLM sometimes claimed 'сохранил 3 рецепта, добавил 18 товаров'
    without any corresponding tool-call — pure hallucination.
    System prompt must forbid this."""
    from sreda.runtime.handlers import _CONVERSATION_SYSTEM_PROMPT

    low = _CONVERSATION_SYSTEM_PROMPT.lower()
    # Accept either "tool-call" language or "не отчитывайся" language.
    assert (
        ("tool-call" in low or "tool call" in low or "вызов tool" in low)
        and ("не отчитыв" in low or "не говори" in low or "только о сделанном" in low)
    ), (
        "System prompt must instruct LLM: if you say 'сохранил X' / "
        "'добавил Y' — there MUST be a tool-call for X and Y in this "
        "SAME turn. No reporting of planned-but-not-executed actions."
    )


def test_plan_week_menu_docstring_warns_about_overwrite():
    tools = _tools()
    desc = tools["plan_week_menu"].description.lower()
    assert "перезапис" in desc or "overwrite" in desc or "replaces" in desc, (
        "plan_week_menu REPLACES the entire week's plan — if the user "
        "incrementally adds one day, the other days get wiped. "
        "Docstring must warn and point to update_menu_item for "
        "incremental changes."
    )


def test_system_prompt_prefers_update_over_remove_add():
    """Prod: LLM did list + remove(2) + add(3) + final text = 4 LLM
    iterations / 32s for a simple regroup. The prompt must point to
    update_shopping_item(s_category) as the cheap path."""
    from sreda.runtime.handlers import _CONVERSATION_SYSTEM_PROMPT

    low = _CONVERSATION_SYSTEM_PROMPT.lower()
    assert "update_shopping_item" in low, (
        "System prompt must instruct LLM to prefer "
        "update_shopping_item / update_shopping_items_category over "
        "remove_shopping_items + add_shopping_items when the user "
        "wants to re-category or rename an existing row. "
        "Each avoided LLM call saves 5–10 seconds."
    )
    # Also must explicitly forbid the remove+add anti-pattern
    assert any(
        phrase in low
        for phrase in ("не делай remove+add", "remove+add", "минимизируй", "не дубли")
    ), (
        "System prompt must explicitly call out the remove+add "
        "anti-pattern and/or duplicate list_* calls."
    )


def test_update_shopping_tools_exposed():
    """New tools must be in the builder's return list — otherwise the
    LLM doesn't know they exist."""
    tools = _tools()
    assert "update_shopping_item" in tools, (
        "update_shopping_item is missing from build_housewife_tools return list"
    )
    assert "update_shopping_items_category" in tools, (
        "update_shopping_items_category is missing from build_housewife_tools return list"
    )


def test_search_recipes_distinguishes_from_menu():
    """LLM confused the recipe book (search_recipes) with the weekly
    menu (list_menu). When user asked 'какое меню на среду?' agent
    saw matching recipe names and said 'меню уже есть' — wrong, those
    were just recipes in the book, not bound to any menu day."""
    tools = _tools()
    desc = tools["search_recipes"].description.lower()
    assert "книг" in desc and ("list_menu" in desc or "не меню" in desc), (
        "search_recipes docstring must clarify that it returns the "
        "WHOLE RECIPE BOOK (all saved recipes, independent of the "
        "weekly menu). To check what's on the menu for a day, use "
        "list_menu — they are different sources of truth."
    )
