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
