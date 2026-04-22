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


def test_chat_turn_timeout_constant_defined():
    """Lock in that the turn-level timeout constant exists and is
    reasonable. Prod observed 20-minute hanging turn; constant caps
    wall time per turn."""
    from sreda.runtime.handlers import CHAT_TURN_TIMEOUT_SECONDS

    # Must be a positive number, and not absurdly small (normal turns
    # take up to 40s) or absurdly large (20-min outlier is what we're
    # protecting against).
    assert isinstance(CHAT_TURN_TIMEOUT_SECONDS, (int, float))
    assert 30 <= CHAT_TURN_TIMEOUT_SECONDS <= 300


def test_admin_logs_has_chat_turn_timeout_filter():
    """Admin UI quick-filter button for CHAT_TURN_TIMEOUT must be
    present so operators can find hung turns without writing grep
    by hand."""
    from pathlib import Path
    tpl = Path("src/sreda/admin/templates/logs.html").read_text(encoding="utf-8")
    assert "CHAT_TURN_TIMEOUT" in tpl, (
        "admin/templates/logs.html must have a quick-filter link "
        "for CHAT_TURN_TIMEOUT — matches the existing "
        "CHAT_EMPTY_REPLY filter pattern."
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


# ---------------------------------------------------------------------------
# Prompt split (2026-04-22) — feature-scoped addons. Core prompt
# should NOT contain food-v1.1 rules; housewife addon MUST add them.
# Keeps non-housewife turns ~500 input tokens lighter per iteration.
# ---------------------------------------------------------------------------


def test_core_prompt_excludes_housewife_food_rules():
    """Generic / non-housewife turns shouldn't pay the food-section
    token tax. Housewife-specific keywords (list_shopping, plan_week_menu,
    recipe sources) must NOT appear in the core prompt."""
    from sreda.runtime.handlers import _CORE_SYSTEM_PROMPT

    low = _CORE_SYSTEM_PROMPT.lower()
    for forbidden in (
        "list_shopping",
        "list_menu",
        "plan_week_menu",
        "save_recipes_batch",
        "user_dictated",
        "огне",  # heat-level rule is housewife-only
    ):
        assert forbidden not in low, (
            f"core prompt must NOT contain {forbidden!r} — it's "
            "housewife-scoped and belongs in _HOUSEWIFE_FOOD_PROMPT, "
            "not in the always-on core that every feature pays for."
        )


def test_build_system_prompt_housewife_includes_food_rules():
    """Full assembled prompt for feature='housewife_assistant' must
    still contain all Stage 7.5 critical rules — they're only moved,
    not deleted."""
    from sreda.runtime.handlers import build_system_prompt

    assembled = build_system_prompt("housewife_assistant").lower()
    for required in (
        "list_shopping",
        "list_menu",
        "search_recipes",
        "plan_week_menu",
        "save_recipes_batch",
        "update_shopping_item",
        "огне",
        "не по памяти",
    ):
        assert required in assembled, (
            f"housewife prompt missing {required!r} — prompt split "
            "dropped a Stage 7.5 rule. Re-check the move from "
            "_CONVERSATION_SYSTEM_PROMPT to _HOUSEWIFE_FOOD_PROMPT."
        )


def test_build_system_prompt_no_feature_returns_core_only():
    """Calling without a feature_key (or with an unknown one) must
    return the core prompt verbatim — no stray feature-addon leaks."""
    from sreda.runtime.handlers import _CORE_SYSTEM_PROMPT, build_system_prompt

    assert build_system_prompt(None) == _CORE_SYSTEM_PROMPT
    assert build_system_prompt("") == _CORE_SYSTEM_PROMPT
    assert build_system_prompt("unknown_feature_xyz") == _CORE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Gemma-4 discipline addendum — shipped 2026-04-22 after prod incident
# where Gemma narrated "я сохранила рецепт" with tools=[] AND leaked
# raw tool-call syntax into the text reply. Addendum tightens the
# tool_calls-vs-text split specifically for Gemma; other models pay
# no token tax.
# ---------------------------------------------------------------------------


def test_gemma_discipline_addendum_fires_for_gemma_model():
    from sreda.runtime.handlers import build_system_prompt

    prompt = build_system_prompt(
        "housewife_assistant", model_name="google/gemma-4-26b-a4b-it",
    )
    low = prompt.lower()
    # Addendum must mention the actual failure modes we observed:
    # tool_calls API vs text-channel, and the "no claim without call"
    # contract.
    assert "tool_calls" in prompt or "tool-call" in low
    assert "никогда не пиши" in low or "не пиши tool" in low
    assert "в этом же ответе" in low or "обязан" in low


def test_gemma_discipline_addendum_skipped_for_mimo():
    """Other providers (MiMo etc) follow the shared rule-set without
    the Gemma tax. Verified by absence of the most specific addendum
    marker phrases."""
    from sreda.runtime.handlers import build_system_prompt

    mimo_prompt = build_system_prompt(
        "housewife_assistant", model_name="mimo-v2-pro",
    )
    gemma_prompt = build_system_prompt(
        "housewife_assistant", model_name="google/gemma-4-26b-a4b-it",
    )
    assert len(gemma_prompt) > len(mimo_prompt), (
        "Gemma prompt must be strictly longer than MiMo prompt due to "
        "the discipline addendum. Regression = addendum logic broken."
    )
    # Addendum's distinctive marker text must NOT appear in MiMo
    assert "строгая дисциплина tool-calls" not in mimo_prompt


def test_prompt_has_recipe_to_shopping_intent_rule():
    """2026-04-22 prod: user asked 'добавь продукты этого рецепта в
    список' → Gemma called save_recipe + search_recipes instead of
    add_shopping_items. Prompt must disambiguate recipe→shopping from
    menu→shopping from save-recipe."""
    from sreda.runtime.handlers import build_system_prompt

    prompt = build_system_prompt("housewife_assistant").lower()
    assert (
        "продукты" in prompt and "этого рецепта" in prompt
        and "add_shopping_items" in prompt
    ), (
        "Housewife prompt must explicitly map 'добавь продукты этого "
        "рецепта в список' to add_shopping_items (NOT save_recipe, "
        "NOT generate_shopping_from_menu)."
    )


def test_prompt_has_title_morphology_rule():
    """2026-04-22 prod: Gemma saved recipe as 'Бараний шурпа' (wrong
    gender) reconstructing nominative from 'бараней шурпы'. Prompt
    must spell out rod agreement for common Russian dish names so the
    model doesn't default to masculine."""
    from sreda.runtime.handlers import build_system_prompt

    prompt = build_system_prompt("housewife_assistant").lower()
    assert "именительном падеже" in prompt
    assert "шурпа" in prompt  # explicit gender example
    # Must mention both masculine and feminine examples to anchor
    assert "мужской" in prompt and "женский" in prompt


def test_gemma_discipline_addendum_skipped_for_no_model_name():
    """Callers that don't know the model yet (bench tools, tests that
    import the prompt standalone) get the baseline — addendum logic
    is opt-in via model_name."""
    from sreda.runtime.handlers import build_system_prompt

    prompt_no_model = build_system_prompt("housewife_assistant")
    prompt_gemma = build_system_prompt(
        "housewife_assistant", model_name="google/gemma-4-26b-a4b-it",
    )
    assert "строгая дисциплина tool-calls" not in prompt_no_model
    assert "строгая дисциплина tool-calls" in prompt_gemma
